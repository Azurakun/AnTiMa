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
                if thread_id in self.active_sessions: del self.active_sessions[thread_id]
        except Exception as e: print(f"Cleanup Error: {e}")

    @tasks.loop(seconds=3)
    async def poll_web_creations(self):
        """Polls for RPGs created via the Web Dashboard."""
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

    async def _initialize_session(self, channel_id, session_db, initial_prompt="Resume", discord_history=None):
        if not self.model or not self.memory_manager: return False
        
        chat_session = self.model.start_chat(history=[])
        # Pass the fetched discord_history to the memory manager
        memory_block = await self.memory_manager.build_context_block(session_db, initial_prompt, recent_history_text=discord_history)
        
        system_prime = (
            f"SYSTEM: BOOTING DUNGEON MASTER CORE.\n"
            f"{memory_block}\n\n"
            f"=== 🛑 IDENTITY & ROLE PROTOCOL ===\n"
            f"1. **YOU ARE THE DUNGEON MASTER (DM):** You describe the world, NPCs, and consequences.\n"
            f"2. **NEVER PLAY AS THE USER:** Do not write the user's dialogue, actions, or internal thoughts. Do not use 'I' unless speaking as an NPC.\n"
            f"3. **PERSPECTIVE:** Address the user as 'You'. (e.g., 'You see a dark cave...', NOT 'I walk into the cave...').\n"
            f"4. **NARRATIVE STYLE:** detailed, immersive, and atmospheric. Write like a novel. Describe the surroundings (lighting, smells, sounds) and NPC mannerisms in detail. Do not be brief.\n"
            f"5. **HANDLING USER DIALOGUE:** You may quote the user's dialogue exactly to weave it into the narrative (e.g., '\"Hello,\" you say, stepping forward...'). You may split their dialogue with descriptions. You MUST NOT alter their words or invent new lines for them.\n"
            f"6. **PACING:** End your turn by inviting the user to act. Do not resolve the entire adventure in one message.\n\n"
            f"=== 🛑 MEMORY MANAGEMENT PROTOCOLS ===\n"
            f"You are responsible for maintaining the STRUCTURED WORLD STATE using the `update_world_entity` tool.\n"
            f"**1. CLASSIFY INFORMATION:**\n"
            f"   - **'Quest':** When a new objective is given, completed, or failed. (e.g. 'Find the key')\n"
            f"   - **'NPC':** New people or significant updates to existing ones. (Use format: Race | Gender | App | Role)\n"
            f"   - **'Location':** When moving to a NEW area. (e.g. 'The Dark Cave')\n"
            f"   - **'Event':** Major plot points or boss kills. (e.g. 'Defeated the Dragon')\n"
            f"**2. PRIORITIZE:**\n"
            f"   - Always update Quests and Locations immediately.\n"
            f"   - Do not rely on the 'Fallback Memory' for current objectives; store them explicitly.\n"
        )
        try: await chat_session.send_message_async(system_prime)
        except Exception as e: print(f"Failed to prime memory: {e}")

        self.active_sessions[channel_id] = {
            'session': chat_session, 'owner_id': session_db['owner_id'], 'last_prompt': initial_prompt
        }
        return True

    async def _scan_narrative_for_entities(self, thread_id, narrative_text):
        """
        Background Scribe: Analyzes DM output to auto-detect and update World Entities.
        Strictly enforces IDENTITY RESOLUTION to prevent duplicates.
        """
        try:
            scribe_session = self.model.start_chat(history=[])
            analysis_prompt = (
                f"SYSTEM: You are the WORLD SCRIBE. PERFOMING A FULL AUDIT.\n"
                f"**MANDATORY INSTRUCTION:** You must read the narrative provided below and extract **EVERY SINGLE** World Entity mentioned.\n"
                f"**PRIORITY:** Do not skip any NPC, Location, or Quest, no matter how minor. If it has a name or description, it MUST be registered.\n\n"
                f"**IMPORTANT DATA STRUCTURE:**\n"
                f"1. **`details`**: **MANDATORY FOR ALIASES**. Store Titles, Aliases, and a short summary here. (e.g. 'Known as Strider. A ranger from the north.').\n"
                f"2. **`attributes`** (NPCs): Populate with 'race', 'gender', 'age', 'appearance', 'personality', 'relationships', 'bio', 'state', 'condition'.\n"
                f"   - **`aliases`**: Extract a LIST of short names/titles. (e.g. ['Strider', 'Elessar', 'The King']).\n"
                f"   - **`age`**: **STRICTLY NUMERIC**. Use a single number (e.g. '25') or a range (e.g. '40-50') if visually estimated. **NEVER** use words like 'Adult', 'Child', or 'Elder'.\n"
                f"   - **`relationships`**: A concise text description of their dynamic with the User (e.g. 'Wary Ally', 'Bitter Enemy'). **Output as a String only. DO NOT use objects.**\n"
                f"3. **`attributes`** (QUESTS): 'issuer', 'trigger', 'rewards', 'status' (active/completed/failed).\n"
                f"   - **DEFINITION**: A Quest is a concrete objective assigned to the player (e.g. 'Retrieve the Amulet', 'Defeat the Goblin King').\n"
                f"   - **IGNORE**: General motivations (e.g. 'Wants to survive') or simple interactions.\n\n"
                f"**IDENTITY RESOLUTION PROTOCOL (CRITICAL):**\n"
                f"1. **NO DUPLICATES:** Always use the **TRUE FULL NAME** as the `name` field.\n"
                f"2. **HANDLE ALIASES:** If an NPC is revealed to be someone else (e.g., 'The Stranger is Aragorn'), DO NOT create a new entry for the Alias.\n"
                f"   - **Right:** Name='Aragorn', attributes={{'aliases': ['The Stranger']}}\n"
                f"   - **Wrong:** Name='The Stranger'.\n"
                f"3. **MERGE:** If an existing NPC appears, use their existing TRUE NAME to update their record.\n"
                f"**NARRATIVE TO ANALYZE:**\n{narrative_text}"
            )
            response = await scribe_session.send_message_async(analysis_prompt)
            
            if response.parts:
                for part in response.parts:
                    if part.function_call:
                        fn = part.function_call
                        if fn.name == "update_world_entity":
                            # Convert protobuf Struct to dict for attributes
                            attrs = {}
                            if "attributes" in fn.args:
                                attrs = dict(fn.args["attributes"])
                            
                            # Extract explicit status from attributes if Scribe put it there
                            status = fn.args.get("status", "active")
                            if "status" in attrs:
                                status = attrs["status"]
                            
                            tools.update_world_entity(
                                str(thread_id), 
                                fn.args["category"], 
                                fn.args["name"], 
                                fn.args["details"], 
                                status, 
                                attributes=attrs
                            )
        except Exception as e:
            print(f"[SCRIBE ERROR] {e}")

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
            "scenario_type": scenario_name, "lore": lore,
            "campaign_log": [], "turn_history": [], "npc_registry": [], "quest_log": [], 
            "created_at": datetime.utcnow(), "last_active": datetime.utcnow(), "active": True, 
            "delete_requested": False, "story_mode": story_mode,
            "total_turns": 0 # Initialize counter
        }
        rpg_sessions_collection.insert_one(session_data)
        
        if respond: await respond(f"✅ Adventure **{title}** created! Check {thread.mention}")
        else: await channel.send(f"⚔️ **New Web-Created Adventure:** {owner.mention} begins **{title}**! -> {thread.mention}")

        mechanics = "2. **Story Mode Active:** NO DICE." if story_mode else "2. **Standard Mode:** Use `roll_d20` for risks."
        
        sys_prompt = (
            f"You are the DM. **SCENARIO:** {scenario_name}. **LORE:** {lore}. {mechanics}\n"
            f"**INSTRUCTION:** Start the adventure now. \n"
            f"1. **Set the Scene:** Write a detailed, immersive opening. Paint the environment with sensory details (sight, sound, smell). Write like a novel. Do not be brief.\n"
            f"2. **Hook:** Present the immediate situation or threat based on the Backstory.\n"
            f"3. **Style:** Narrative prose. Not a list.\n"
            f"4. **Perspective:** 2nd Person ('You...').\n"
            f"5. **Constraint:** Do NOT act for the player. Stop and wait for their input."
        )
        
        # Initial turn - no history yet
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

        # This decrements the counter in memory.py
        self.memory_manager.delete_last_turn(thread_id)
        
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

        if thread_id in self.active_sessions:
            del self.active_sessions[thread_id]

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
            # --- 1. REAL-TIME CONTEXT FETCHING ---
            discord_context_str = ""
            try:
                raw_msgs = [m async for m in channel.history(limit=6)]
                raw_msgs.reverse()
                
                context_lines = []
                for msg in raw_msgs:
                    if msg.content == "🧠 **The Dungeon Master is thinking...**": continue
                    speaker = "The Dungeon Master" if msg.author.id == self.bot.user.id else msg.author.display_name
                    content = msg.clean_content
                    if not content and msg.embeds and msg.author.id == self.bot.user.id:
                        content = msg.embeds[0].description
                    if content:
                        context_lines.append(f"[{speaker}]: {content}")
                
                discord_context_str = "\n".join(context_lines)
            except Exception as e:
                print(f"Context Fetch Error: {e}")
                discord_context_str = None

            await self.memory_manager.archive_old_turns(channel.id, session_db)
            
            await self._initialize_session(channel.id, session_db, prompt, discord_history=discord_context_str)
            
            data = self.active_sessions[channel.id]
            chat_session = data['session']
            rpg_sessions_collection.update_one({"thread_id": channel.id}, {"$set": {"last_active": datetime.utcnow()}})
            
            # --- FIX: STATIC TURN CALCULATION USING PERSISTENT COUNTER ---
            # Fallback to len(turn_history) if 'total_turns' is missing (for older sessions)
            existing_turns = session_db.get("total_turns", len(session_db.get("turn_history", [])))
            current_turn_id = existing_turns + 1

            if not is_reroll: data['last_prompt'] = prompt; data['last_roll_result'] = None
            
            async with channel.typing():
                story_mode = session_db.get("story_mode", False)
                mechanics_instr = "**MODE: STORY**" if story_mode else "**MODE: STANDARD**"
                
                full_prompt = (
                    f"**USER ACTION:** {prompt}\n"
                    f"**DM INSTRUCTIONS:**\n"
                    f"{mechanics_instr}\n"
                    f"1. **ROLEPLAY:** Narrate in **high detail** (Sensory details, atmosphere). Write like a novel. Do not be brief.\n"
                    f"   - Describe the environment, sounds, and smells.\n"
                    f"   - If the user spoke, you may quote them exactly to integrate it, but DO NOT change their words.\n"
                    f"   - Do NOT speak for the user or describe their internal thoughts.\n"
                    f"2. **NPCS:** If you introduce or update an NPC, use `update_world_entity`.\n"
                    f"   - Use the `attributes` parameter for deep details (bio, relationships).\n"
                    f"   - **ALIASES:** If they have known aliases, pass them as a list in `attributes['aliases']`.\n"
                    f"   - Keep the `details` parameter short (1 sentence summary).\n"
                    f"   - **Make note of any RELATIONSHIP changes** in the narrative so the Scribe detects them.\n"
                    f"3. **CONSISTENCY CHECK:** BEFORE generating, consult the **NPC REGISTRY**. You MUST adhere to the **RELATIONSHIPS** defined there. Do not make a hostile NPC suddenly friendly.\n"
                    f"4. **TOOLS:** Use `update_world_entity` to track everything.\n"
                    f"{'Reroll requested.' if is_reroll else ''}"
                )
                
                # --- ROBUST API CALL LOOP WITH MALFORMED RECOVERY ---
                response = None
                max_retries = 2
                attempt = 0
                
                while attempt <= max_retries:
                    try:
                        if attempt == 0:
                            response = await chat_session.send_message_async(full_prompt)
                        else:
                            response = await chat_session.send_message_async("SYSTEM: The previous function call was MALFORMED. Please try again with valid arguments.")
                        break 
                    except Exception as e:
                        if "MALFORMED_FUNCTION_CALL" in str(e) or "finish_reason: MALFORMED_FUNCTION_CALL" in str(e):
                            attempt += 1
                            print(f"Malformed call detected. Retrying ({attempt}/{max_retries})...")
                            if attempt > max_retries: raise e 
                        else:
                            raise e 

                turns = 0
                text_content = ""
                
                while response.parts and response.parts[0].function_call and turns < 10:
                    turns += 1
                    fn = response.parts[0].function_call
                    res_txt = "Error"
                    
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
                            if "attributes" in fn.args:
                                attrs = dict(fn.args["attributes"])
                            res_txt = tools.update_world_entity(
                                str(channel.id), fn.args["category"], fn.args["name"], 
                                fn.args["details"], fn.args.get("status", "active"), attributes=attrs
                            )
                        elif fn.name == "grant_item_to_player": res_txt = tools.grant_item_to_player(fn.args["user_id"], fn.args["item_name"], fn.args["description"])
                        elif fn.name == "apply_damage": res_txt = "Story Mode." if story_mode else tools.apply_damage(str(channel.id), fn.args["user_id"], fn.args["damage_amount"])
                        elif fn.name == "apply_healing": res_txt = "Story Mode." if story_mode else tools.apply_healing(str(channel.id), fn.args["user_id"], fn.args["heal_amount"])
                        elif fn.name == "deduct_mana": res_txt = "Story Mode." if story_mode else tools.deduct_mana(str(channel.id), fn.args["user_id"], fn.args["mana_cost"])
                        elif fn.name == "update_journal": res_txt = tools.update_journal(str(channel.id), fn.args.get("log_entry"))
                    except Exception as tool_err:
                        res_txt = f"Tool Error: {tool_err}"

                    try:
                        response = await chat_session.send_message_async(genai.protos.Content(parts=[genai.protos.Part(function_response=genai.protos.FunctionResponse(name=fn.name, response={'result': res_txt}))]))
                    except Exception as e:
                        if "MALFORMED_FUNCTION_CALL" in str(e) or "finish_reason: MALFORMED_FUNCTION_CALL" in str(e):
                             try:
                                response = await chat_session.send_message_async("SYSTEM: Previous output malformed. Please output the narrative text now.")
                             except:
                                text_content = "**[System]** Critical Error in Narrative Generation."
                                break
                        else:
                             raise e

                try: text_content = response.text
                except ValueError: text_content = "" 

                if not text_content.strip():
                    try:
                        force_resp = await chat_session.send_message_async("System: Tool execution confirmed. Now provide the narrative description.")
                        text_content = force_resp.text
                    except: text_content = "**[System Notice]** Narrative generation failed."

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

                # Pass the calculated turn ID to save it persistently
                self.memory_manager.save_turn(channel.id, user.name if user else "System", prompt, text_content, user_message_id=message_id, bot_message_id=bot_message_ids, current_turn_id=current_turn_id)
                
                # --- AUTO-DETECT ENTITIES (SCRIBE) ---
                asyncio.create_task(self._scan_narrative_for_entities(channel.id, text_content))
                
                if user: limiter.consume(user.id, channel.guild.id, "rpg_gen")
        except Exception as e:
            if processing_msg:
                try: await processing_msg.delete()
                except: pass
            await channel.send(f"⚠️ Game Error: {e}")
            print(f"RPG Error: {e}")

    async def close_session(self, thread_id, channel):
        rpg_sessions_collection.update_one({"thread_id": thread_id}, {"$set": {"active": False, "ended_at": datetime.utcnow()}})
        if thread_id in self.active_sessions: del self.active_sessions[thread_id]
        try: await channel.send("📕 **Adventure Archived.**"); await channel.edit(archived=True, locked=True)
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
        # ACTIVE QUESTS
        active_qs = [q for q in quests.values() if q.get("status") == "active"]
        if active_qs:
            emb = discord.Embed(title="🛡️ Active Objectives", color=discord.Color.gold())
            for q in active_qs:
                emb.add_field(name=q['name'], value=q['details'], inline=False)
            embeds.append(emb)

        # COMPLETED QUESTS
        completed_qs = [q for q in quests.values() if q.get("status") == "completed"]
        if completed_qs:
            emb = discord.Embed(title="✅ Completed Log", color=discord.Color.green())
            for q in completed_qs[:5]: # Limit to 5 recently completed
                emb.add_field(name=q['name'], value=q['details'], inline=False)
            if len(completed_qs) > 5: emb.set_footer(text=f"...and {len(completed_qs)-5} more.")
            embeds.append(emb)

        # NPCS
        if npcs:
            emb = discord.Embed(title="👥 Known Contacts", color=discord.Color.blue())
            count = 0
            for npc in list(npcs.values()):
                if npc.get("status") != "active": continue
                if count >= 5: break
                
                # Fetch Attributes
                attrs = npc.get("attributes", {})
                state = attrs.get("state", "Alive")
                cond = attrs.get("condition", "Unknown")
                details = npc.get('details', 'No data').split('|')[0]
                
                # ALIAS FORMATTING FOR EMBED
                aliases = attrs.get("aliases", [])
                alias_display = " ".join([f"`{a}`" for a in aliases]) if aliases else "No aliases known."

                # Format as requested by user
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
        
        try:
            # 1. Fetch History & Reconstruct Turns
            raw_messages = [m async for m in interaction.channel.history(limit=None, oldest_first=True)]
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

            # --- FIX: Set total_turns based on full reconstructed length ---
            total_count = len(reconstructed_turns)
            rpg_sessions_collection.update_one({"thread_id": interaction.channel.id}, {"$set": {
                "turn_history": reconstructed_turns,
                "total_turns": total_count 
            }})

            # 2. Vector Indexing
            cleaned_history = []
            full_text_log = ""
            for msg in raw_messages:
                if msg.author.bot and msg.author.id != self.bot.user.id: continue 
                content = msg.content or (msg.embeds[0].description if msg.embeds else "")
                if content: 
                    cleaned_history.append({"author": msg.author.name, "content": content, "timestamp": msg.created_at})
                    full_text_log += f"{msg.author.name}: {content}\n"

            if not cleaned_history: return await interaction.followup.send("⚠️ No history.")

            await self.memory_manager.clear_thread_vectors(interaction.channel.id)
            chunks = await self.memory_manager.batch_ingest_history(interaction.channel.id, cleaned_history)
            
            # 3. WIPE ALL WORLD STATE (Strict Rebuild)
            rpg_world_state_collection.update_one(
                {"thread_id": interaction.channel.id}, 
                {"$set": {
                    "quests": {},
                    "npcs": {},
                    "locations": {},
                    "events": {}
                }}
            )

            # 4. Retroactive Entity Extraction (Full History)
            # Pass full_text_log without limit to ensure absolute completeness
            asyncio.create_task(self._scan_narrative_for_entities(interaction.channel.id, full_text_log))
            
            view = discord.ui.View()
            url = f"{WEB_DASHBOARD_URL}/rpg/inspect/{interaction.channel.id}"
            view.add_item(discord.ui.Button(label="🧠 Check Inspector", url=url, style=discord.ButtonStyle.link))

            await interaction.followup.send(f"✅ **Sync Complete:**\n- 📜 Fixed **{total_count}** Turns.\n- 🗂️ Indexed **{chunks}** Memories.\n- 🧹 **World State Wiped & Rebuilding...**\n- 🧠 Retroactive Full Audit Queued.", view=view)
            
        except Exception as e: await interaction.followup.send(f"❌ Error: {e}")

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
        
        # trim_history will now manage total_turns decrement/set logic if implemented in memory.py
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