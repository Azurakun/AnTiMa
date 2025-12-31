# cogs/rpg_system/cog.py
import discord
from discord import app_commands
from discord.ext import commands, tasks
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import asyncio
import random
import uuid
import re 
from datetime import datetime
from utils.db import (
    ai_config_collection, 
    rpg_sessions_collection, 
    rpg_inventory_collection, 
    rpg_world_state_collection,
    web_actions_collection, 
    rpg_web_tokens_collection,
    db 
)
from utils.limiter import limiter
from .config import RPG_CLASSES
from . import tools
from .ui import RPGGameView, AdventureSetupView, CloseVoteView
from .memory import RPGContextManager
from . import prompts # <--- IMPORTED PROMPTS

# BASE URL for the Web Dashboard
WEB_DASHBOARD_URL = "https://antima-production.up.railway.app"

class RPGAdventureCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.model = None
        self.memory_manager = None
        self.safety_settings = {
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
                    tools.roll_d20, tools.update_journal,
                    tools.update_world_entity
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

    def _log_debug(self, thread_id, level, message, details=None):
        try:
            db.rpg_debug_terminal.insert_one({
                "thread_id": str(thread_id),
                "timestamp": datetime.utcnow(),
                "level": level,  
                "message": message,
                "details": details or {}
            })
        except Exception as e: print(f"Log Error: {e}")

    # --- AGE SANITIZATION HELPER ---
    def _sanitize_age(self, age_input):
        if not age_input: return "Unknown"
        s = str(age_input).strip().lower()
        if re.search(r'\d', s):
            clean = re.search(r'[\d\-\s\+<>]+', s)
            return clean.group(0).strip() if clean else s
        if "child" in s or "kid" in s or "young" in s: return "10"
        if "teen" in s: return "16"
        if "adult" in s: return "30"
        if "middle" in s: return "45"
        if "old" in s or "elder" in s or "ancient" in s: return "70"
        return "Unknown" 

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
                rpg_world_state_collection.delete_one({"thread_id": thread_id})
                db.rpg_debug_terminal.delete_many({"thread_id": str(thread_id)}) 
                if thread_id in self.active_sessions: del self.active_sessions[thread_id]
        except Exception as e: print(f"Cleanup Error: {e}")

    @tasks.loop(seconds=3)
    async def poll_web_creations(self):
        try:
            actions = list(web_actions_collection.find({"type": "create_rpg_web", "status": "pending"}))
            for action in actions:
                try:
                    web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "processing"}})
                    guild = self.bot.get_guild(action["guild_id"])
                    user = guild.get_member(action["user_id"]) if guild else None
                    if user and guild:
                        data = action["data"]
                        c = data["character"]
                        profile = {
                            "name": c.get("name"), "class": c.get("class"), "hp": 100, "max_hp": 100, "mp": 50, "max_mp": 50,
                            "stats": c.get("stats"), "skills": ["Custom Action"], "alignment": c.get("alignment"),
                            "backstory": c.get("backstory"), "age": c.get("age"), "pronouns": c.get("pronouns", "They/Them"),
                            "appearance": c.get("appearance", "Unknown"), "personality": c.get("personality", "Unknown"),
                            "hobbies": c.get("hobbies", "None")
                        }
                        await self.create_adventure_thread(
                            interaction=None, lore=data["lore"], players=[user], profiles={user.id: profile},
                            scenario_name=data["scenario"], story_mode=data["story_mode"],
                            custom_title=data["title"], manual_guild_id=guild.id, manual_user=user
                        )
                        web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "completed"}})
                    else:
                        web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "failed", "reason": "User/Guild not found"}})
                except Exception as e:
                    print(f"Error processing web RPG: {e}")
                    web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "error", "error": str(e)}})
        except Exception as e: print(f"Poller Error: {e}")

    async def _initialize_session(self, channel_id, session_db, initial_prompt="Resume"):
        if not self.model or not self.memory_manager: return False
        
        chat_session = self.model.start_chat(history=[])
        memory_block, debug_data = await self.memory_manager.build_context_block(session_db, initial_prompt)
        
        self._log_debug(channel_id, "system", "Initializing Session Context", details={"debug_data": debug_data})
        
        # [MODIFIED] Using Imported Prompt
        system_prime = prompts.SYSTEM_PRIME.format(memory_block=memory_block)

        try: await chat_session.send_message_async(system_prime)
        except Exception as e: print(f"Failed to prime memory: {e}")

        self.active_sessions[channel_id] = {
            'session': chat_session, 'owner_id': session_db['owner_id'], 'last_prompt': initial_prompt
        }
        return True

    async def _scan_narrative_for_entities(self, thread_id, narrative_text):
        chunk_preview = narrative_text[:30].replace('\n', ' ')
        self._log_debug(thread_id, "system", "SCRIBE: Analyzing narrative chunk", details={"preview": chunk_preview})

        scribe_session = self.model.start_chat(history=[])
        
        # [MODIFIED] Using Imported Prompt
        analysis_prompt = prompts.SCRIBE_ANALYSIS.format(narrative_text=narrative_text)

        max_retries = 2
        attempt = 0
        response = None

        while attempt <= max_retries:
            try:
                if attempt == 0: response = await scribe_session.send_message_async(analysis_prompt)
                else: response = await scribe_session.send_message_async("SYSTEM: Previous call MALFORMED. Retry with valid arguments.")
                break
            except Exception as e:
                if "MALFORMED_FUNCTION_CALL" in str(e) or "finish_reason: MALFORMED_FUNCTION_CALL" in str(e):
                    attempt += 1
                    if attempt > max_retries: return 
                else: return

        try:
            entities_found = 0
            new_location_detected = False
            npcs_in_this_scene = []

            if response.parts:
                for part in response.parts:
                    if part.function_call and part.function_call.name == "update_world_entity":
                        if part.function_call.args["category"] == "Location":
                            new_location_detected = True
                        if part.function_call.args["category"] == "NPC":
                            npcs_in_this_scene.append(part.function_call.args["name"])

                if new_location_detected:
                    self._log_debug(thread_id, "system", "SCRIBE: 🌍 Scene Change Detected. Dismissing absent NPCs.")
                    world_data = rpg_world_state_collection.find_one({"thread_id": int(thread_id)}) or {}
                    all_npcs = world_data.get("npcs", {})
                    
                    for npc_key, npc_data in all_npcs.items():
                        if npc_data.get("status") == "active" and npc_data["name"] not in npcs_in_this_scene:
                            rpg_world_state_collection.update_one(
                                {"thread_id": int(thread_id)},
                                {"$set": {f"npcs.{npc_key}.status": "background"}}
                            )
                            self._log_debug(thread_id, "info", f"SCRIBE: Auto-dismissed NPC '{npc_data['name']}' to background.")

                for part in response.parts:
                    if part.function_call:
                        fn = part.function_call
                        if fn.name == "update_world_entity":
                            entities_found += 1
                            attrs = {}
                            if "attributes" in fn.args: attrs = dict(fn.args["attributes"])
                            
                            rel = attrs.get("relationships") or attrs.get("relationship")
                            if rel: attrs["relationships"] = str(rel) if not isinstance(rel, list) else ", ".join(rel)

                            status = fn.args.get("status", "active")
                            if "status" in attrs: status = attrs["status"]

                            if "age" in attrs: attrs["age"] = self._sanitize_age(attrs["age"])
                            
                            self._log_debug(thread_id, "system", f"SCRIBE: Indexed {fn.args.get('category')} -> {fn.args.get('name')}")

                            tools.update_world_entity(
                                str(thread_id), fn.args["category"], fn.args["name"], 
                                fn.args["details"], status, attributes=attrs
                            )
            self._log_debug(thread_id, "info", f"SCRIBE: Finished. Entities Found: {entities_found}")

        except Exception as e:
            self._log_debug(thread_id, "error", f"SCRIBE PROCESS ERROR: {e}")

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
        if not config:
            if respond: await respond("Configuration not found for this guild.")
            return

        channel = self.bot.get_channel(config.get("rpg_channel_id"))
        if not channel:
            if respond: await respond("RPG Channel not set!")
            return

        if custom_title: title = custom_title
        else:
            try:
                # [MODIFIED] Using Imported Prompt
                prompt = prompts.TITLE_GENERATION.format(scenario=scenario_name, lore=lore[:100])
                resp = await self.model.generate_content_async(prompt)
                title = resp.text.strip().replace('"', '')[:50]
            except: title = f"Quest: {owner.name}"

        thread = await channel.create_thread(name=title, type=discord.ChannelType.private_thread, auto_archive_duration=10080)
        for p in players: await thread.add_user(p)
        
        player_stats_db = {str(p.id): profiles.get(p.id, RPG_CLASSES["Freelancer"]) for p in players}
        
        session_data = {
            "thread_id": thread.id, "guild_id": guild_id, "owner_id": owner.id, "owner_name": owner.name,
            "title": title, "players": [p.id for p in players], "player_stats": player_stats_db, 
            "scenario_type": scenario_name, "lore": lore,
            "campaign_log": [], "turn_history": [], "npc_registry": [], "quest_log": [], 
            "created_at": datetime.utcnow(), "last_active": datetime.utcnow(), "active": True, 
            "delete_requested": False, "story_mode": story_mode,
            "total_turns": 0 
        }
        rpg_sessions_collection.insert_one(session_data)
        
        if respond: await respond(f"✅ Adventure **{title}** created! Check {thread.mention}")
        else: await channel.send(f"⚔️ **New Web-Created Adventure:** {owner.mention} begins **{title}**! -> {thread.mention}")

        self._log_debug(thread.id, "system", f"ADVENTURE CREATED: {title}", details={"scenario": scenario_name, "mode": "story" if story_mode else "standard"})

        mechanics = "2. **Story Mode Active:** NO DICE." if story_mode else "2. **Standard Mode:** Use `roll_d20` for risks."
        
        # [MODIFIED] Using Imported Prompt
        sys_prompt = prompts.ADVENTURE_START.format(scenario_name=scenario_name, lore=lore, mechanics=mechanics)
        
        await self._initialize_session(thread.id, session_data, "Start")
        await self.process_game_turn(thread, sys_prompt)

    async def reroll_turn_callback(self, interaction, thread_id):
        session_db = rpg_sessions_collection.find_one({"thread_id": thread_id})
        if not session_db or interaction.user.id != session_db['owner_id']:
             return await interaction.followup.send("Leader only.", ephemeral=True)
        try: await interaction.message.delete()
        except: pass 
        
        last_turn = None
        if session_db.get("turn_history"):
            last_turn = session_db["turn_history"][-1]

        self.memory_manager.delete_last_turn(thread_id)
        self._log_debug(thread_id, "info", "Turn Rerolled by User.")
        
        prompt = "Continue" 
        msg_id = None
        if last_turn:
            prompt = last_turn.get("input", "Continue")
            msg_id = last_turn.get("user_message_id")
            if msg_id:
                try:
                    original_msg = await interaction.channel.fetch_message(int(msg_id))
                    if original_msg and original_msg.content:
                        prompt = f"{original_msg.author.name}: {original_msg.content}"
                except Exception: pass

        if thread_id in self.active_sessions: del self.active_sessions[thread_id]
        await self.process_game_turn(interaction.channel, prompt, is_reroll=True, message_id=msg_id)

    async def process_game_turn(self, channel, prompt, user=None, is_reroll=False, message_id=None):
        if not self.model or not self.memory_manager: return await channel.send("⚠️ RPG System not ready (Model Error).")
        if user and not limiter.check_available(user.id, channel.guild.id, "rpg_gen"): return await channel.send("⏳ Quota Exceeded.")
        session_db = rpg_sessions_collection.find_one({"thread_id": channel.id})
        if not session_db: return
        
        processing_msg = None
        try: processing_msg = await channel.send("🧠 **The Dungeon Master is thinking...**")
        except: pass

        try:
            self._log_debug(channel.id, "info", f"Processing Turn: {prompt[:50]}...")
            await self.memory_manager.archive_old_turns(channel.id, session_db)
            await self._initialize_session(channel.id, session_db, prompt)
            
            data = self.active_sessions[channel.id]
            chat_session = data['session']
            rpg_sessions_collection.update_one({"thread_id": channel.id}, {"$set": {"last_active": datetime.utcnow()}})
            
            existing_turns = session_db.get("total_turns", len(session_db.get("turn_history", [])))
            current_turn_id = existing_turns + 1

            if not is_reroll: data['last_prompt'] = prompt; data['last_roll_result'] = None
            
            async with channel.typing():
                story_mode = session_db.get("story_mode", False)
                mechanics_instr = "**MODE: STORY**" if story_mode else "**MODE: STANDARD**"
                reroll_instr = "Reroll requested." if is_reroll else ""
                
                # [MODIFIED] Using Imported Prompt
                full_prompt = prompts.GAME_TURN.format(
                    user_action=prompt,
                    mechanics_instruction=mechanics_instr,
                    reroll_instruction=reroll_instr
                )
                
                self._log_debug(channel.id, "ai", "Thinking...", details={"constructed_prompt": full_prompt[:200]+"..."})

                response = None
                max_retries = 2
                attempt = 0
                while attempt <= max_retries:
                    try:
                        if attempt == 0: response = await chat_session.send_message_async(full_prompt)
                        else: response = await chat_session.send_message_async("SYSTEM: The previous function call was MALFORMED. Please try again with valid arguments.")
                        break 
                    except Exception as e:
                        if "MALFORMED_FUNCTION_CALL" in str(e) or "finish_reason: MALFORMED_FUNCTION_CALL" in str(e):
                            attempt += 1
                            if attempt > max_retries: raise e 
                        else: raise e 

                turns = 0
                text_content = ""
                while response.parts and response.parts[0].function_call and turns < 10:
                    turns += 1
                    fn = response.parts[0].function_call
                    res_txt = "Error"
                    self._log_debug(channel.id, "system", f"TOOL: {fn.name}", details=dict(fn.args))

                    try:
                        if fn.name == "roll_d20":
                            if story_mode: res_txt = "Dice disabled."
                            elif is_reroll and data.get('last_roll_result'): res_txt = f"LOCKED: {data['last_roll_result']}"
                            else:
                                diff, mod = int(fn.args.get("difficulty", 10)), int(fn.args.get("modifier", 0))
                                roll = random.randint(1, 20); total = roll + mod; success = total >= diff
                                desc = f"🎲 **{roll}** (d20) {f'+ {mod}' if mod >= 0 else f'- {abs(mod)}'} = **{total}** vs DC {diff}"
                                color = discord.Color.green() if success else discord.Color.red()
                                if roll == 20: color = discord.Color.gold(); desc += " **(CRIT!)**"
                                await channel.send(embed=discord.Embed(title=f"🎲 {fn.args.get('check_type', 'Check')}", description=desc, color=color))
                                res_txt = f"Roll: {roll}, Total: {total}, DC: {diff}, Success: {success}"
                                if not is_reroll: data['last_roll_result'] = res_txt
                        elif fn.name == "update_world_entity": 
                            attrs = {}
                            if "attributes" in fn.args: attrs = dict(fn.args["attributes"])
                            if "age" in attrs: attrs["age"] = self._sanitize_age(attrs["age"])
                            res_txt = tools.update_world_entity(
                                str(channel.id), fn.args["category"], fn.args["name"], 
                                fn.args["details"], fn.args.get("status", "active"), attributes=attrs
                            )
                        elif fn.name == "grant_item_to_player": res_txt = tools.grant_item_to_player(fn.args["user_id"], fn.args["item_name"], fn.args["description"])
                        elif fn.name == "apply_damage": res_txt = "Story Mode." if story_mode else tools.apply_damage(str(channel.id), fn.args["user_id"], fn.args["damage_amount"])
                        elif fn.name == "apply_healing": res_txt = "Story Mode." if story_mode else tools.apply_healing(str(channel.id), fn.args["user_id"], fn.args["heal_amount"])
                        elif fn.name == "deduct_mana": res_txt = "Story Mode." if story_mode else tools.deduct_mana(str(channel.id), fn.args["user_id"], fn.args["mana_cost"])
                        elif fn.name == "update_journal": res_txt = tools.update_journal(str(channel.id), fn.args.get("log_entry"))
                    except Exception as tool_err: res_txt = f"Tool Error: {tool_err}"

                    try:
                        response = await chat_session.send_message_async(genai.protos.Content(parts=[genai.protos.Part(function_response=genai.protos.FunctionResponse(name=fn.name, response={'result': res_txt}))]))
                    except Exception as e:
                        if "MALFORMED_FUNCTION_CALL" in str(e) or "finish_reason: MALFORMED_FUNCTION_CALL" in str(e):
                             try: response = await chat_session.send_message_async("SYSTEM: Previous output malformed. Please output the narrative text now.")
                             except: text_content = "**[System]** Critical Error in Narrative Generation."; break
                        else: raise e

                try: text_content = response.text
                except ValueError: text_content = "" 

                if not text_content.strip():
                    try:
                        force_resp = await chat_session.send_message_async("System: Tool execution confirmed. Now provide the narrative description.")
                        text_content = force_resp.text
                    except: text_content = "**[System Notice]** Narrative generation failed."
                
                self._log_debug(channel.id, "ai", "Generated Narrative", details={"length": len(text_content)})

                footer_text = await self.memory_manager.get_token_count_and_footer(chat_session, turn_id=current_turn_id)
                if processing_msg:
                    try: await processing_msg.delete()
                    except: pass
                    processing_msg = None

                chunks = [text_content[i:i+4000] for i in range(0, len(text_content), 4000)] or ["..."]
                bot_message_ids = []

                for i, chunk in enumerate(chunks):
                    is_last = (i == len(chunks) - 1)
                    story_emb = discord.Embed(description=chunk, color=discord.Color.from_rgb(47, 49, 54))
                    if i == 0: story_emb.set_author(name="The Dungeon Master", icon_url=self.bot.user.avatar.url)
                    embeds = [story_emb]
                    view = None
                    if is_last:
                        story_emb.set_footer(text=footer_text)
                        view = RPGGameView(self, channel.id)
                    bot_msg = await channel.send(embeds=embeds, view=view)
                    bot_message_ids.append(bot_msg.id)

                self.memory_manager.save_turn(channel.id, user.name if user else "System", prompt, text_content, user_message_id=message_id, bot_message_id=bot_message_ids, current_turn_id=current_turn_id)
                await self._scan_narrative_for_entities(channel.id, text_content)
                if user: limiter.consume(user.id, channel.guild.id, "rpg_gen")
        except Exception as e:
            if processing_msg:
                try: await processing_msg.delete()
                except: pass
            self._log_debug(channel.id, "error", f"CRITICAL TURN ERROR: {e}")
            await channel.send(f"⚠️ Game Error: {e}")
            print(f"RPG Error: {e}")

    # --- COMMANDS (Identical to previous, excluded for brevity as they are unchanged) ---
    async def close_session(self, thread_id, channel):
        rpg_sessions_collection.update_one({"thread_id": thread_id}, {"$set": {"active": False, "ended_at": datetime.utcnow()}})
        if thread_id in self.active_sessions: del self.active_sessions[thread_id]
        try: await channel.send("📕 **Adventure Archived.**"); await channel.edit(archived=True, locked=True)
        except: pass

    rpg_group = app_commands.Group(name="rpg", description="⚔️ Play immersive role-playing adventures.")

    @rpg_group.command(name="start", description="Start a new adventure.")
    async def rpg_start(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False) 
        config = ai_config_collection.find_one({"_id": str(interaction.guild_id)})
        if not config or "rpg_channel_id" not in config: 
            return await interaction.followup.send("⚠️ Admin must set channel first using `/config rpg`.", ephemeral=True)
        view = AdventureSetupView(self.bot, interaction.user)
        view.message = await interaction.followup.send(content=f"⚔️ **RPG Lobby** {interaction.user.mention}", embed=view._get_party_embed(), view=view)

    @rpg_group.command(name="world", description="Inspect the AI's World Knowledge & Open Memory Panel.")
    async def rpg_world(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread): 
            return await interaction.response.send_message("Use this inside an active Adventure Thread.", ephemeral=True)
        
        session = rpg_sessions_collection.find_one({"thread_id": interaction.channel.id})
        if not session: return await interaction.response.send_message("No session data found.", ephemeral=True)
        
        world_data = rpg_world_state_collection.find_one({"thread_id": interaction.channel.id})
        quests = world_data.get("quests", {}) if world_data else {}
        npcs = world_data.get("npcs", {}) if world_data else {}

        await interaction.response.defer(ephemeral=True)

        embeds = []
        active_qs = [q for q in quests.values() if q.get("status") == "active"]
        if active_qs:
            emb = discord.Embed(title="🛡️ Active Objectives", color=discord.Color.gold())
            for q in active_qs: emb.add_field(name=q['name'], value=q['details'], inline=False)
            embeds.append(emb)

        completed_qs = [q for q in quests.values() if q.get("status") == "completed"]
        if completed_qs:
            emb = discord.Embed(title="✅ Completed Log", color=discord.Color.green())
            for q in completed_qs[:5]: emb.add_field(name=q['name'], value=q['details'], inline=False)
            if len(completed_qs) > 5: emb.set_footer(text=f"...and {len(completed_qs)-5} more.")
            embeds.append(emb)

        if npcs:
            emb = discord.Embed(title="👥 Known Contacts", color=discord.Color.blue())
            count = 0
            for npc in list(npcs.values()):
                if npc.get("status") != "active": continue
                if count >= 5: break
                
                attrs = npc.get("attributes", {})
                state = attrs.get("state", "Alive")
                cond = attrs.get("condition", "Unknown")
                details = npc.get('details', 'No data').split('|')[0]
                aliases = attrs.get("aliases", [])
                alias_display = " ".join([f"`{a}`" for a in aliases]) if aliases else "No aliases known."

                val_text = (
                    f"{details}\n"
                    f"**Aliases:** {alias_display}\n"
                    f"**Last Seen State:** {state}\n"
                    f"**Last Seen Condition:** {cond}"
                )
                emb.add_field(name=f"👤 {npc['name']}", value=val_text, inline=False)
                count += 1
                
            if len(npcs) > 5: emb.set_footer(text=f"... and {len(npcs)-5} more (See Inspector)")
            embeds.append(emb)

        view = discord.ui.View()
        url = f"{WEB_DASHBOARD_URL}/rpg/inspect/{interaction.channel.id}"
        view.add_item(discord.ui.Button(label="🧠 Open Memory Inspector", url=url, style=discord.ButtonStyle.link, emoji="🔗"))

        msg_content = "🌍 **World State Summary**"
        if not embeds: msg_content += "\n*(No major discoveries yet. Check the web inspector for details)*"
        
        await interaction.followup.send(content=msg_content, embeds=embeds, view=view)

    @rpg_group.command(name="sync", description="🔄 Re-reads and indexes history. Also scans for missing Entities!")
    async def rpg_sync(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread): return await interaction.response.send_message("Threads only.", ephemeral=True)
        session = rpg_sessions_collection.find_one({"thread_id": interaction.channel.id})
        if not session or interaction.user.id != session.get('owner_id'): return await interaction.response.send_message("Host only.", ephemeral=True)
        
        await interaction.response.send_message("🔄 **Syncing Memory, Turn Sequence, & World State...**")
        self._log_debug(interaction.channel.id, "system", "INIT_SYNC: Sync requested by user.")
        
        try:
            self._log_debug(interaction.channel.id, "info", "SYNC: Fetching Message History...")
            raw_messages = [m async for m in interaction.channel.history(limit=None, oldest_first=True)]
            self._log_debug(interaction.channel.id, "info", f"SYNC: Fetched {len(raw_messages)} messages.")

            reconstructed_turns = []
            current_user_msg = None
            for msg in raw_messages:
                if msg.author.id == self.bot.user.id:
                    if msg.embeds and current_user_msg:
                        turn_data = {
                            "timestamp": msg.created_at, "user_name": current_user_msg.author.name,
                            "input": current_user_msg.content or "Action", "output": msg.embeds[0].description,
                            "user_message_id": current_user_msg.id, "bot_message_id": msg.id
                        }
                        reconstructed_turns.append(turn_data)
                        current_user_msg = None
                elif not msg.author.bot:
                    current_user_msg = msg

            total_count = len(reconstructed_turns)
            self._log_debug(interaction.channel.id, "info", f"SYNC: Reconstructed {total_count} turns.")
            rpg_sessions_collection.update_one({"thread_id": interaction.channel.id}, {"$set": {
                "turn_history": reconstructed_turns,
                "total_turns": total_count 
            }})

            self._log_debug(interaction.channel.id, "info", "SYNC: Building Vector Index...")
            cleaned_history = []
            for msg in raw_messages:
                if msg.author.bot and msg.author.id != self.bot.user.id: continue 
                content = msg.content or (msg.embeds[0].description if msg.embeds else "")
                if content: 
                    cleaned_history.append({"author": msg.author.name, "content": content, "timestamp": msg.created_at})

            if not cleaned_history: return await interaction.followup.send("⚠️ No history.")

            await self.memory_manager.clear_thread_vectors(interaction.channel.id)
            chunks = await self.memory_manager.batch_ingest_history(interaction.channel.id, cleaned_history)
            self._log_debug(interaction.channel.id, "info", f"SYNC: Indexed {chunks} memory chunks.")
            
            self._log_debug(interaction.channel.id, "warn", "SYNC: Wiping World State for clean rebuild.")
            rpg_world_state_collection.update_one(
                {"thread_id": interaction.channel.id}, 
                {"$set": {"quests": {}, "npcs": {}, "locations": {}, "events": {}}}
            )

            self._log_debug(interaction.channel.id, "info", "SYNC: Starting Batch Entity Extraction...")
            chunk_size = 15000 
            current_chunk = ""
            scan_tasks = []
            chunk_count = 0

            for msg in raw_messages:
                author = msg.author.name
                content = msg.content or (msg.embeds[0].description if msg.embeds else "")
                if content:
                    line = f"[{author}]: {content}\n"
                    if len(current_chunk) + len(line) > chunk_size:
                        chunk_count += 1
                        self._log_debug(interaction.channel.id, "info", f"SYNC: Dispatching Audit Task #{chunk_count} ({len(current_chunk)} chars)")
                        scan_tasks.append(self._scan_narrative_for_entities(interaction.channel.id, current_chunk))
                        current_chunk = ""
                    current_chunk += line
            
            if current_chunk:
                chunk_count += 1
                self._log_debug(interaction.channel.id, "info", f"SYNC: Dispatching Final Audit Task #{chunk_count} ({len(current_chunk)} chars)")
                scan_tasks.append(self._scan_narrative_for_entities(interaction.channel.id, current_chunk))

            self._log_debug(interaction.channel.id, "system", f"SYNC: Complete. Running {len(scan_tasks)} background audits.")
            for t in scan_tasks: asyncio.create_task(t)
            
            view = discord.ui.View()
            url = f"{WEB_DASHBOARD_URL}/rpg/inspect/{interaction.channel.id}"
            view.add_item(discord.ui.Button(label="🧠 Check Inspector", url=url, style=discord.ButtonStyle.link))

            await interaction.followup.send(f"✅ **Sync Complete:**\n- 📜 Fixed **{total_count}** Turns.\n- 🗂️ Indexed **{chunks}** Memories.\n- 🧹 **World State Wiped.**\n- 🧠 **{len(scan_tasks)} Batch Audits** queued (Background).", view=view)
            
        except Exception as e: 
            self._log_debug(interaction.channel.id, "error", f"SYNC CRITICAL ERROR: {e}")
            await interaction.followup.send(f"❌ Error: {e}")

    @rpg_group.command(name="web_new", description="Create an adventure via the Web Dashboard.")
    async def rpg_web_new(self, interaction: discord.Interaction):
        token = str(uuid.uuid4())
        rpg_web_tokens_collection.insert_one({"token": token, "user_id": interaction.user.id, "guild_id": interaction.guild_id, "status": "pending", "created_at": datetime.utcnow()})
        url = f"{WEB_DASHBOARD_URL}/rpg/setup?token={token}"
        embed = discord.Embed(title="🌐 Web Setup", description=f"[**Click Here**]({url})", color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rpg_group.command(name="personas", description="Manage your saved characters via the Web Dashboard.")
    async def rpg_personas(self, interaction: discord.Interaction):
        token = str(uuid.uuid4())
        rpg_web_tokens_collection.insert_one({"token": token, "user_id": interaction.user.id, "guild_id": interaction.guild_id, "status": "pending", "type": "persona_management", "created_at": datetime.utcnow()})
        url = f"{WEB_DASHBOARD_URL}/rpg/personas?token={token}"
        embed = discord.Embed(title="🎭 Persona Manager", description=f"[**Click Here**]({url})", color=discord.Color.purple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rpg_group.command(name="history", description="View turn history to find a rewind point.")
    async def rpg_history(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread): return await interaction.response.send_message("Threads only.", ephemeral=True)
        session = rpg_sessions_collection.find_one({"thread_id": interaction.channel.id})
        if not session or "turn_history" not in session: return await interaction.response.send_message("No history.", ephemeral=True)
        history = session["turn_history"]
        desc = ""
        for i in range(max(0, len(history) - 10), len(history)):
            desc += f"**Turn {i+1}**: {history[i]['input'][:50]}...\n"
        await interaction.response.send_message(embed=discord.Embed(title="📜 History", description=desc, color=discord.Color.blue()), ephemeral=True)

    @rpg_group.command(name="rewind", description="Rewind the story to a specific turn (Deletes messages & Memory).")
    async def rpg_rewind(self, interaction: discord.Interaction, turn_id: int):
        if not isinstance(interaction.channel, discord.Thread): return await interaction.response.send_message("Threads only.", ephemeral=True)
        session = rpg_sessions_collection.find_one({"thread_id": interaction.channel.id})
        if not session or interaction.user.id != session.get('owner_id'): return await interaction.response.send_message("Host only.", ephemeral=True)
        
        target_index = turn_id - 1
        history = session.get("turn_history", [])
        if target_index < 0 or target_index >= len(history): return await interaction.response.send_message("Invalid Turn ID.", ephemeral=True)

        await interaction.response.send_message(f"⏳ **Rewinding to Turn {turn_id}...** (Wiping Future Memory)", ephemeral=True)
        self._log_debug(interaction.channel.id, "warn", f"Rewind to turn {turn_id} requested.")
        
        deleted_turns, rewind_timestamp = self.memory_manager.trim_history(interaction.channel.id, target_index)
        if rewind_timestamp: await self.memory_manager.purge_memories_since(interaction.channel.id, rewind_timestamp)

        if deleted_turns:
            for turn in deleted_turns:
                try: 
                    if turn.get("user_message_id"): await (await interaction.channel.fetch_message(int(turn["user_message_id"]))).delete()
                except: pass
                b_ids = turn.get("bot_message_id")
                if b_ids:
                    if isinstance(b_ids, list):
                        for bid in b_ids:
                            try: await (await interaction.channel.fetch_message(bid)).delete()
                            except: pass
                    else:
                        try: await (await interaction.channel.fetch_message(int(b_ids))).delete()
                        except: pass
        
        if interaction.channel.id in self.active_sessions: del self.active_sessions[interaction.channel.id]
        await interaction.followup.send(f"✅ Rewind Complete! State reverted to **{rewind_timestamp.strftime('%H:%M:%S')}**.", ephemeral=True)

    @rpg_group.command(name="mode", description="Switch Game Mode.")
    @app_commands.choices(mode=[app_commands.Choice(name="Standard", value="standard"), app_commands.Choice(name="Story", value="story")])
    async def rpg_mode(self, interaction: discord.Interaction, mode: str):
        rpg_sessions_collection.update_one({"thread_id": interaction.channel.id}, {"$set": {"story_mode": (mode=="story")}})
        await interaction.response.send_message(f"Mode: **{mode.upper()}**")

    @rpg_group.command(name="end", description="End session.")
    async def rpg_end(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread): return await interaction.response.send_message("Threads only.", ephemeral=True)
        session = rpg_sessions_collection.find_one({"thread_id": interaction.channel.id})
        if not session: return
        if interaction.user.id == session.get("owner_id"): await self.close_session(interaction.channel.id, interaction.channel); return
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
            await self.process_game_turn(message.channel, f"{message.author.name}: {message.content}", message.author, message_id=message.id)