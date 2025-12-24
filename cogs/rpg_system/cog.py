# cogs/rpg_system/cog.py
import discord
from discord import app_commands
from discord.ext import commands, tasks
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import asyncio
import random
import uuid
from datetime import datetime
from utils.db import (
    ai_config_collection, 
    rpg_sessions_collection, 
    rpg_inventory_collection,
    db 
)
from utils.limiter import limiter
from .config import RPG_CLASSES
from . import tools
from .ui import RPGGameView, AdventureSetupView, CloseVoteView
from .memory import RPGContextManager

# Define collections locally if not in utils.db
web_actions_collection = db["web_actions"]
rpg_web_tokens_collection = db["rpg_web_tokens"]

class RPGAdventureCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
        try:
            self.model = genai.GenerativeModel(
                'gemini-3-pro-preview',
                tools=[
                    tools.grant_item_to_player, tools.apply_damage, 
                    tools.apply_healing, tools.deduct_mana, 
                    tools.roll_d20, tools.update_journal
                ],
                safety_settings=self.safety_settings
            )
            self.memory_manager = RPGContextManager(self.model)
            self.active_sessions = {} 
        except Exception as e:
            print(f"Failed to load Gemini for RPG: {e}")
        
        self.cleanup_deleted_sessions.start()
        self.poll_web_creations.start()

    def cog_unload(self):
        self.cleanup_deleted_sessions.cancel()
        self.poll_web_creations.cancel()

    @tasks.loop(seconds=10)
    async def cleanup_deleted_sessions(self):
        try:
            to_delete = rpg_sessions_collection.find({"delete_requested": True})
            for session in to_delete:
                thread_id = session['thread_id']
                try:
                    thread = self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
                    if thread: await thread.delete()
                except: pass
                rpg_sessions_collection.delete_one({"thread_id": thread_id})
                if thread_id in self.active_sessions: del self.active_sessions[thread_id]
        except Exception as e: print(f"Cleanup Error: {e}")

    @tasks.loop(seconds=3)
    async def poll_web_creations(self):
        """Checks DB for RPGs created via the Website."""
        try:
            actions = list(web_actions_collection.find({"type": "create_rpg_web", "status": "pending"}))
            for action in actions:
                try:
                    web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "processing"}})
                    
                    guild = self.bot.get_guild(action["guild_id"])
                    user = guild.get_member(action["user_id"]) if guild else None
                    
                    if user and guild:
                        data = action["data"]
                        char_data = data["character"]
                        
                        profile = {
                            "class": char_data["class"],
                            "hp": 100, "max_hp": 100, "mp": 50, "max_mp": 50,
                            "stats": char_data["stats"],
                            "skills": ["Custom Action"],
                            "alignment": char_data["alignment"],
                            "backstory": char_data["backstory"],
                            "age": char_data["age"]
                        }
                        
                        await self.create_adventure_thread(
                            interaction=None,
                            lore=data["lore"],
                            players=[user],
                            profiles={user.id: profile},
                            scenario_name=data["scenario"],
                            story_mode=data["story_mode"],
                            custom_title=data["title"],
                            manual_guild_id=guild.id,
                            manual_user=user
                        )
                        web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "completed"}})
                    else:
                        web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "failed", "reason": "User/Guild not found"}})
                except Exception as e:
                    print(f"Error processing web RPG: {e}")
                    web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "error", "error": str(e)}})
        except Exception as e:
            print(f"Poller Error: {e}")

    async def _initialize_session(self, channel_id, session_db):
        chat_session = self.model.start_chat(history=[])
        memory_block = self.memory_manager.build_context_block(session_db)
        
        system_prime = (
            f"SYSTEM: BOOTING DUNGEON MASTER CORE.\n"
            f"{memory_block}\n"
            "**CRITICAL INSTRUCTION: CONTEXTUAL CONTINUITY**\n"
            "1. **Strict Recall:** You are resuming an ongoing story. Rely ONLY on the provided Memory Block.\n"
            "2. **NPC Protocol:** Do NOT mention names of NPCs unless they appear in the Memory Block. If not found, treat them as strangers described by appearance.\n"
            "3. **Tone:** Maintain the exact narrative style of the previous logs.\n"
            "4. Wait for the User's input to continue."
        )
        try: await chat_session.send_message_async(system_prime)
        except Exception as e: print(f"Failed to prime memory: {e}")

        self.active_sessions[channel_id] = {
            'session': chat_session, 
            'owner_id': session_db['owner_id'], 
            'last_prompt': "Resume"
        }
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
            
            stat_lines = []
            base_stats = stats.get('stats', {})
            for key in ["STR", "DEX", "INT", "CHA"]:
                val = base_stats.get(key, 10)
                mod = (val - 10) // 2
                sign = "+" if mod >= 0 else ""
                stat_lines.append(f"**{key}**: {val} (`{sign}{mod}`)")
            
            embed.add_field(
                name=f"{name} ({stats['class']})", 
                value=(
                    f"**HP:** `{stats['hp']}/{stats['max_hp']}` {hp_bar}\n"
                    f"**MP:** `{stats['mp']}/{stats['max_mp']}` {mp_bar}\n"
                    f"📊 {' | '.join(stat_lines)}\n"
                    f"✨ {', '.join(stats.get('skills', []))}"
                ), inline=False
            )
        return embed

    async def create_adventure_thread(self, interaction, lore, players, profiles, scenario_name, story_mode=False, custom_title=None, manual_guild_id=None, manual_user=None):
        if interaction:
            guild_id = interaction.guild_id
            owner = interaction.user
            respond = interaction.followup.send
        else:
            guild_id = manual_guild_id
            owner = manual_user
            respond = None
            
        config = ai_config_collection.find_one({"_id": str(guild_id)})
        channel = self.bot.get_channel(config.get("rpg_channel_id"))
        
        if not channel:
            if respond: await respond("RPG Channel not set!")
            return

        if custom_title: title = custom_title
        else:
            try:
                prompt = f"Generate a unique 5-word title for an RPG adventure. Scenario: {scenario_name}. Lore: {lore[:100]}."
                resp = await self.model.generate_content_async(prompt)
                title = resp.text.strip().replace('"', '')[:50]
            except: title = f"Quest: {owner.name}"

        thread = await channel.create_thread(name=title, type=discord.ChannelType.private_thread, auto_archive_duration=10080)
        for p in players: await thread.add_user(p)

        player_stats_db = {str(p.id): profiles.get(p.id, RPG_CLASSES["Freelancer"]) for p in players}

        session_data = {
            "thread_id": thread.id, "guild_id": guild_id, "owner_id": owner.id, "owner_name": owner.name,
            "title": title, "players": [p.id for p in players], "player_stats": player_stats_db, 
            "scenario_type": scenario_name, "campaign_log": [], "turn_history": [], "npc_registry": [], "quest_log": [], 
            "created_at": datetime.utcnow(), "last_active": datetime.utcnow(), "active": True, 
            "delete_requested": False, "story_mode": story_mode
        }
        rpg_sessions_collection.insert_one(session_data)
        
        if respond: await respond(f"✅ Adventure **{title}** created! Check {thread.mention}")
        else: await channel.send(f"⚔️ **New Web-Created Adventure:** {owner.mention} begins **{title}**! -> {thread.mention}")

        char_details = []
        for pid, pdata in player_stats_db.items():
            detail = f"Player {pid}: {pdata.get('class')} (Alignment: {pdata.get('alignment', 'N/A')})"
            if 'backstory' in pdata: detail += f"\n   - Backstory: {pdata['backstory']}"
            char_details.append(detail)

        mechanics = (
            "2. **Story Mode Active:** NO DICE. Narrative focus only. Do not track HP/MP." 
            if story_mode else 
            "2. **Standard Mode:** Use `roll_d20` for risks. Track HP/MP/Ammo via tools."
        )

        sys_prompt = (
            f"You are the **Dungeon Master** for a fictional tabletop RPG. "
            f"**SCENARIO:** {scenario_name}\n"
            f"**GUIDELINES:**\n"
            f"1. **Fiction Only:** Combat/Conflict allowed.\n"
            f"{mechanics}\n"
            f"3. **Memory:** Use `update_journal`.\n"
            f"4. **Immersion:** Be descriptive.\n"
            f"**LORE:** {lore}\n"
            f"**PARTY DETAILS:**\n{'\n'.join(char_details)}\n"
            f"**START:** The players have gathered. Set the opening scene vividly."
        )
        
        await self._initialize_session(thread.id, session_data)
        await self.process_game_turn(thread, sys_prompt)

    async def reroll_turn_callback(self, interaction, thread_id):
        session_db = rpg_sessions_collection.find_one({"thread_id": thread_id})
        if not session_db or interaction.user.id != session_db['owner_id']:
             return await interaction.followup.send("Leader only.", ephemeral=True)
        
        if thread_id in self.active_sessions:
            data = self.active_sessions[thread_id]
            try: data['session'].rewind() 
            except: pass
            await interaction.channel.send("🎲 **Rewinding Fate...**")
            await self.process_game_turn(interaction.channel, data.get('last_prompt', "Continue"), is_reroll=True)

    async def process_game_turn(self, channel, prompt, user=None, is_reroll=False):
        if user and not limiter.check_available(user.id, channel.guild.id, "rpg_gen"):
            return await channel.send("⏳ Quota Exceeded.")

        session_db = rpg_sessions_collection.find_one({"thread_id": channel.id})
        if not session_db: return
        
        if channel.id not in self.active_sessions:
            await self._initialize_session(channel.id, session_db)
        
        data = self.active_sessions[channel.id]
        chat_session = data['session']
        rpg_sessions_collection.update_one({"thread_id": channel.id}, {"$set": {"last_active": datetime.utcnow()}})
        
        if not is_reroll:
            data['last_prompt'] = prompt
            data['last_roll_result'] = None
        
        async with channel.typing():
            story_mode = session_db.get("story_mode", False)
            mechanics_instr = (
                "**MODE: STORY (No Dice/Stats).** Focus on narrative." 
                if story_mode else 
                "**MODE: STANDARD.** Use `roll_d20` for checks. Use tools for HP/MP."
            )
            full_prompt = (
                f"**USER ACTION:** {prompt}\n"
                f"**DM INSTRUCTIONS:**\n"
                f"{mechanics_instr}\n"
                f"1. **STRICT NPC PROTOCOL:** Check memory. If NPC is NOT in memory, treat as STRANGER (no name). Use `update_journal` if new name revealed.\n"
                f"2. **IMMERSION:** Describe sights/sounds vividly.\n"
                f"3. **OUTPUT:** Narrate the outcome. Do NOT leave blank.\n"
                f"{'Reroll requested. Change the outcome.' if is_reroll else ''}"
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
                        if story_mode: res_txt = "Dice disabled in Story Mode."
                        elif is_reroll and data.get('last_roll_result'): res_txt = f"LOCKED: {data['last_roll_result']}"
                        else:
                            diff, mod = int(fn.args.get("difficulty", 10)), int(fn.args.get("modifier", 0))
                            roll = random.randint(1, 20)
                            total = roll + mod
                            success = total >= diff
                            desc = f"🎲 **{roll}** (d20) {f'+ {mod}' if mod >= 0 else f'- {abs(mod)}'} = **{total}** vs DC {diff}"
                            color = discord.Color.green() if success else discord.Color.red()
                            if roll == 20: 
                                color = discord.Color.gold()
                                desc += " **(CRIT!)**"
                            elif roll == 1: desc += " **(FAIL!)**"
                            await channel.send(embed=discord.Embed(title=f"🎲 {fn.args.get('check_type', 'Check')}", description=desc, color=color))
                            res_txt = f"Roll: {roll}, Total: {total}, DC: {diff}, Success: {success}"
                            if not is_reroll: data['last_roll_result'] = res_txt

                    elif fn.name == "grant_item_to_player": res_txt = tools.grant_item_to_player(fn.args["user_id"], fn.args["item_name"], fn.args["description"])
                    elif fn.name == "apply_damage": res_txt = "Story Mode." if story_mode else tools.apply_damage(str(channel.id), fn.args["user_id"], fn.args["damage_amount"])
                    elif fn.name == "apply_healing": res_txt = "Story Mode." if story_mode else tools.apply_healing(str(channel.id), fn.args["user_id"], fn.args["heal_amount"])
                    elif fn.name == "deduct_mana": res_txt = "Story Mode." if story_mode else tools.deduct_mana(str(channel.id), fn.args["user_id"], fn.args["mana_cost"])
                    elif fn.name == "update_journal": res_txt = tools.update_journal(str(channel.id), fn.args.get("log_entry"), fn.args.get("npc_update"), fn.args.get("quest_update"))
                    
                    response = await chat_session.send_message_async(genai.protos.Content(parts=[genai.protos.Part(function_response=genai.protos.FunctionResponse(name=fn.name, response={'result': res_txt}))]))

                try: text_content = response.text
                except ValueError: text_content = "" 

                if not text_content or not text_content.strip():
                    try:
                        force_resp = await chat_session.send_message_async("System Notice: You executed the mechanics, now DESCRIBE the narrative outcome vividly. Do not leave it blank.")
                        text_content = force_resp.text
                    except Exception as e:
                        text_content = f"**[System Notice]** Narrative unavailable (Safety Filter). Action executed."

                self.memory_manager.save_turn(channel.id, user.name if user else "System", prompt, text_content)
                footer_text = await self.memory_manager.get_token_count_and_footer(chat_session)
                
                view = RPGGameView(self, channel.id)
                story_emb = discord.Embed(description=text_content, color=discord.Color.from_rgb(47, 49, 54))
                story_emb.set_author(name="The Dungeon Master", icon_url=self.bot.user.avatar.url if self.bot.user.avatar else None)
                story_emb.set_footer(text=footer_text)

                embeds = [story_emb]
                if not story_mode:
                    stat_emb = self.get_status_embed(channel.id)
                    if stat_emb: embeds.append(stat_emb)

                await channel.send(embeds=embeds, view=view)
                if user: limiter.consume(user.id, channel.guild.id, "rpg_gen")
            except Exception as e:
                await channel.send(f"⚠️ Game Error: {e}")
                print(f"RPG Error: {e}")

    async def close_session(self, thread_id, channel):
        rpg_sessions_collection.update_one({"thread_id": thread_id}, {"$set": {"active": False, "ended_at": datetime.utcnow()}})
        if thread_id in self.active_sessions: del self.active_sessions[thread_id]
        try:
            await channel.send("📕 **Adventure Archived.**")
            await channel.edit(archived=True, locked=True)
        except: pass

    # --- COMMANDS ---
    rpg_group = app_commands.Group(name="rpg", description="⚔️ Play immersive role-playing adventures.")

    @rpg_group.command(name="start", description="Start a new adventure.")
    async def rpg_start(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False) 
        config = ai_config_collection.find_one({"_id": str(interaction.guild_id)})
        if not config or "rpg_channel_id" not in config: 
            return await interaction.followup.send("⚠️ Admin must set channel first using `/config rpg`.", ephemeral=True)
        view = AdventureSetupView(self.bot, interaction.user)
        view.message = await interaction.followup.send(content=f"⚔️ **RPG Lobby** {interaction.user.mention}", embed=view._get_party_embed(), view=view)

    @rpg_group.command(name="web_new", description="Create an adventure via the Web Dashboard (Detailed Setup).")
    async def rpg_web_new(self, interaction: discord.Interaction):
        token = str(uuid.uuid4())
        rpg_web_tokens_collection.insert_one({
            "token": token, "user_id": interaction.user.id, "guild_id": interaction.guild_id,
            "status": "pending", "created_at": datetime.utcnow()
        })
        # CHANGE TO YOUR DOMAIN
        dashboard_url = "http://localhost:8000" 
        url = f"{dashboard_url}/rpg/setup?token={token}"
        
        embed = discord.Embed(title="🌐 Web Setup Initiated", description="Design your adventure with detailed lore, stats, and character backstory.", color=discord.Color.blue())
        embed.add_field(name="Setup Link", value=f"[**Click Here to Create Adventure**]({url})")
        embed.set_footer(text="Link expires once used.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rpg_group.command(name="mode", description="Switch Game Mode.")
    @app_commands.choices(mode=[app_commands.Choice(name="Standard", value="standard"), app_commands.Choice(name="Story", value="story")])
    async def rpg_mode(self, interaction: discord.Interaction, mode: str):
        if not isinstance(interaction.channel, discord.Thread): return await interaction.response.send_message("Threads only.", ephemeral=True)
        rpg_sessions_collection.update_one({"thread_id": interaction.channel.id}, {"$set": {"story_mode": (mode=="story")}})
        await interaction.response.send_message(f"Game Mode switched to **{mode.upper()}**.")

    @rpg_group.command(name="end", description="End session.")
    async def rpg_end(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread): return await interaction.response.send_message("Threads only.", ephemeral=True)
        session = rpg_sessions_collection.find_one({"thread_id": interaction.channel.id})
        if not session: return await interaction.response.send_message("No session.", ephemeral=True)
        
        if interaction.user.id == session.get("owner_id"):
             # FIX: Respond FIRST, then archive.
             await interaction.response.send_message("🛑 **Stopping adventure...**")
             await self.close_session(interaction.channel.id, interaction.channel)
             return
        
        view = CloseVoteView(self, interaction.channel.id, interaction.user.id, session.get("players", []), session['owner_id'])
        await interaction.response.send_message(f"Vote to end?", view=view)

    @rpg_group.command(name="inventory", description="Check inventory.")
    async def rpg_inventory(self, interaction: discord.Interaction, user: discord.Member = None):
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
        session = rpg_sessions_collection.find_one({"thread_id": message.channel.id})
        if session and message.author.id in session.get("players", []):
            await self.process_game_turn(message.channel, f"{message.author.name}: {message.content}", message.author)