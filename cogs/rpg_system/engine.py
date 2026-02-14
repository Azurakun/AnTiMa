# cogs/rpg_system/engine.py
import discord
import asyncio
import random
import traceback
import re  # <--- NEW IMPORT
from datetime import datetime, timezone
import google.generativeai as genai
from utils.db import (
    rpg_sessions_collection, rpg_world_state_collection, 
    ai_config_collection, rpg_vector_memory_collection
)
from utils.limiter import limiter
from .config import RPG_CLASSES
from . import prompts, tools
from .utils import RPGLogger, StatusManager, sanitize_age
from .ui import RPGGameView

class RPGEngine:
    def __init__(self, bot, model, memory_manager, scribe_model):
        self.bot = bot
        self.model = model
        self.scribe_model = scribe_model
        self.memory_manager = memory_manager
        self.active_sessions = {} 
        self.scribe_locks = {}

    async def get_or_create_session(self, channel_id, session_db, initial_prompt="Resume"):
        if channel_id in self.active_sessions:
            return self.active_sessions[channel_id]['session']
        await self.initialize_session(channel_id, session_db, initial_prompt)
        return self.active_sessions[channel_id]['session']

    async def initialize_session(self, channel_id, session_db, initial_prompt="Resume"):
        await RPGLogger.broadcast(channel_id, "INIT", "Booting Context Manager", {"prompt": initial_prompt})
        
        chat_session = self.model.start_chat(history=[])
        
        memory_block, debug_data = await self.memory_manager.build_context_block(
            session_db, initial_prompt, logger=RPGLogger.log
        )
        
        active_npcs = debug_data.get("world_entities", {}).get("active_npcs", [])

        RPGLogger.log(channel_id, "system", "Context Built", details={
            "token_count_approx": len(memory_block)/4,
            "components": list(debug_data.keys()),
            "rag_hits": debug_data.get("rag_hits_count", 0),
            "active_npcs": active_npcs
        })
        
        system_prime = prompts.SYSTEM_PRIME.format(memory_block=memory_block)
        try:
            await chat_session.send_message_async(system_prime)
            RPGLogger.log(channel_id, "system", "System Prime Accepted.")
        except Exception as e:
            RPGLogger.log(channel_id, "error", f"System Prime Failed: {e}")
            return False

        self.active_sessions[channel_id] = {
            'session': chat_session,
            'owner_id': session_db['owner_id'],
            'last_prompt': initial_prompt,
            'active_npcs': active_npcs
        }
        return True

    async def process_turn(self, channel, prompt, user=None, is_reroll=False, message_id=None):
        if not self.model: return await channel.send("⚠️ RPG System Offline.")
        
        session_db = rpg_sessions_collection.find_one({"thread_id": channel.id})
        if not session_db: return

        # --- 1. INITIALIZE STATUS MANAGER ---
        processing_msg = await channel.send("🧠 **Reading Campaign History...**")
        status = StatusManager(processing_msg)

        try:
            await RPGLogger.broadcast(channel.id, "START_TURN", "Processing User Input", {"input": prompt})
            
            # --- ARCHIVE & INIT ---
            await self.memory_manager.archive_old_turns(channel.id, session_db)
            chat_session = await self.get_or_create_session(channel.id, session_db, prompt)
            session_data = self.active_sessions.get(channel.id)

            # --- DYNAMIC STATUS: CONTEXT ---
            active_npcs = session_data.get('active_npcs', [])
            if active_npcs:
                await status.set(f"Recalling {', '.join(active_npcs[:2])}...")
            else:
                await status.set("Scanning World State...")

            current_turn_id = session_db.get("total_turns", 0) + 1

            # --- 2. CONSTRUCT HUD (State Injection) ---
            world_data = rpg_world_state_collection.find_one({"thread_id": channel.id}) or {}
            
            players = session_db.get("player_stats", {})
            p_data = list(players.values())[0] if players else {}
            hp_str = f"{p_data.get('hp', 0)}/{p_data.get('max_hp', 100)}"
            mp_str = f"{p_data.get('mp', 0)}/{p_data.get('max_mp', 50)}"
            
            locs = world_data.get("locations", {})
            active_loc = next((l['name'] for l in locs.values() if l.get('status') == 'active'), "Unknown")
            
            quests = world_data.get("quests", {})
            active_q = next((q['name'] for q in quests.values() if q.get('status') == 'active'), "None")
            
            env = world_data.get("environment", {})
            time_str = env.get("time", "Day")

            hud_update = (
                f"[SYSTEM STATE UPDATE: Turn {current_turn_id} | Time: {time_str}]\n"
                f"[LOCATION: {active_loc} | QUEST: {active_q}]\n"
                f"[STATUS: {p_data.get('name', 'Player')} - HP {hp_str} | MP {mp_str}]\n"
                "(Remind the user of these stats ONLY if relevant to the action.)"
            )

            # --- 3. PACING ANALYSIS ---
            story_mode = session_db.get("story_mode", False)
            mechanics_instr = "**MODE: STORY**" if story_mode else "**MODE: STANDARD**"
            reroll_instr = "Reroll requested." if is_reroll else ""
            
            passive_keywords = ["...", "wait", "nothing", "silence", "stares", "blinks", "hmm", "listen"]
            action_indicators = ["attack", "cast", "shoot", "run", "go", "use", "look", "grab", "dodge", "check", "climb", "break"]
            
            prompt_lower = prompt.lower()
            is_short = len(prompt) < 15
            is_keyword = any(k == prompt_lower.strip() for k in passive_keywords)
            contains_action = any(verb in prompt_lower for verb in action_indicators)
            is_passive = (is_short or is_keyword) and not contains_action
            
            # --- DYNAMIC STATUS: PACING ---
            await status.set("Analyzing Scene Pacing...")
            
            if contains_action or is_reroll:
                pacing = "FAST / INTENSE. Short, punchy sentences. Focus on movement, impact, and visceral sensation. Adrenaline."
            elif is_short:
                pacing = "NEUTRAL. Keep the flow moving. React naturally to the brevity."
            else:
                pacing = "SLOW / ATMOSPHERIC. Focus on nuance, subtext, and rich sensory depth. Let the moment breathe."

            social_pressure = ""
            if is_passive and not is_reroll:
                social_pressure = (
                    "\n🚨 **SOCIAL PRESSURE TRIGGER:**\n"
                    "The User is silent/passive. NPCs MUST react to this silence.\n"
                    "- Friendly NPCs: Check in (\"Everything okay?\").\n"
                    "- Hostile/Busy NPCs: Get annoyed or aggressive.\n"
                    "- DO NOT describe the silence. MAKE THE WORLD ACT."
                )
            
            if not is_reroll:
                session_data['last_prompt'] = prompt
                session_data['last_roll_result'] = None

            full_prompt = f"{hud_update}\n\n" + prompts.GAME_TURN.format(
                user_action=prompt,
                mechanics_instruction=mechanics_instr,
                pacing=pacing,
                reroll_instruction=reroll_instr + social_pressure
            )
            
            # --- DYNAMIC STATUS: PROMPTING ---
            await status.set("Drafting Narrative...")
            
            await RPGLogger.broadcast(channel.id, "PROMPTING", "Sending Prompt to Model", {"length": len(full_prompt)})

            async with channel.typing():
                response = await self._safe_generate(chat_session, full_prompt)
                
                turns = 0
                text_content = ""
                
                # --- 4. TOOL EXECUTION LOOP ---
                while response.parts and response.parts[0].function_call and turns < 10:
                    turns += 1
                    fn = response.parts[0].function_call
                    
                    if fn.name == "roll_d20": await status.set("🎲 Rolling Dice...")
                    elif fn.name == "update_world_entity": await status.set("📝 Updating World...")
                    elif fn.name == "grant_item_to_player": await status.set("🎒 Managing Inventory...")
                    else: await status.set(f"🔧 Executing {fn.name}...")

                    await RPGLogger.broadcast(channel.id, "TOOL_CALL", f"Executing {fn.name}", {"args": dict(fn.args)})
                    
                    res_txt = await self._execute_tool(channel, fn, story_mode, is_reroll, session_data)
                    
                    await RPGLogger.broadcast(channel.id, "TOOL_RESULT", f"Result for {fn.name}", {"output": res_txt})
                    
                    response = await self._safe_generate(chat_session, genai.protos.Content(
                        parts=[genai.protos.Part(function_response=genai.protos.FunctionResponse(
                            name=fn.name, response={'result': res_txt}
                        ))]
                    ))

                # --- 5. TEXT EXTRACTION ---
                try:
                    text_content = response.text
                except ValueError:
                    await status.set("⚠️ Model Error. Retrying...")
                    await RPGLogger.broadcast(channel.id, "WARN", "Model failed to output text. Forcing summary.")
                    try:
                        fallback_resp = await chat_session.send_message_async(
                            "SYSTEM: Tool execution finished. You MUST now provide the narrative description. Do not call any more tools."
                        )
                        text_content = fallback_resp.text
                    except Exception as e:
                        text_content = f"**[System]** Critical Error: {str(e)}"

                if not text_content: text_content = "**[System]** Narrative generation failed (Empty Response)."
                
                # --- 6. FINAL CLEANUP & SEND ---
                await status.set("✍️ Finalizing...")
                await RPGLogger.broadcast(channel.id, "NARRATIVE_GEN", "Generating Final Response", {"length": len(text_content)})

                # Remove thinking msg
                await status.delete()

                bot_msg_ids = await self._send_narrative(channel, text_content, chat_session, current_turn_id)

                self.memory_manager.save_turn(
                    channel.id, user.name if user else "System", prompt, text_content, 
                    user_message_id=message_id, bot_message_id=bot_msg_ids, current_turn_id=current_turn_id
                )
                
                active_list = session_data.get('active_npcs', [])
                self.bot.loop.create_task(self._run_scribe(channel.id, text_content, active_list))
                
                await self.memory_manager.snapshot_world_state(channel.id, current_turn_id)
                
                if user: limiter.consume(user.id, channel.guild.id, "rpg_gen")
                
                await RPGLogger.broadcast(channel.id, "TURN_COMPLETE", "Turn Finished", {"turn_id": current_turn_id})

        except Exception as e:
            await status.delete()
            RPGLogger.log(channel.id, "error", f"CRITICAL ERROR: {e}", details={"trace": traceback.format_exc()})
            await channel.send(f"⚠️ **Game Error:** {e}")

    # --- SYNC LOGIC (Unchanged) ---
    async def sync_session(self, channel, status_msg):
        # ... (Same as previous, omitted for brevity) ...
        try:
            RPGLogger.log(channel.id, "info", "SYNC: Fetching Message History...")
            raw_messages = [m async for m in channel.history(limit=None, oldest_first=True)]
            reconstructed_turns = []
            current_turn = None 
            for msg in raw_messages:
                naive_ts = msg.created_at.astimezone(timezone.utc).replace(tzinfo=None)
                if msg.author.bot and msg.author.id == self.bot.user.id:
                    if current_turn:
                        content = msg.content or (msg.embeds[0].description if msg.embeds else "")
                        if content: current_turn['bot_parts'].append(content)
                        current_turn['bot_msg_ids'].append(msg.id)
                elif not msg.author.bot:
                    if current_turn and current_turn['bot_parts']:
                        full_output = "\n".join(current_turn['bot_parts'])
                        reconstructed_turns.append({
                            "timestamp": current_turn['timestamp'],
                            "user_name": current_turn['user_name'],
                            "input": current_turn['input'],
                            "output": full_output,
                            "user_message_id": current_turn['user_id'],
                            "bot_message_id": current_turn['bot_msg_ids'],
                            "turn_id": len(reconstructed_turns) + 1
                        })
                    current_turn = {'user_name': msg.author.name, 'input': msg.content, 'user_id': msg.id, 'timestamp': naive_ts, 'bot_parts': [], 'bot_msg_ids': []}
            if current_turn and current_turn['bot_parts']:
                full_output = "\n".join(current_turn['bot_parts'])
                reconstructed_turns.append({"timestamp": current_turn['timestamp'], "user_name": current_turn['user_name'], "input": current_turn['input'], "output": full_output, "user_message_id": current_turn['user_id'], "bot_message_id": current_turn['bot_msg_ids'], "turn_id": len(reconstructed_turns) + 1})

            total_count = len(reconstructed_turns)
            await status_msg.edit(content=f"🔄 **Syncing...** [2/4] 🧩 Reconstructed {total_count} turns. Archiving...")
            rpg_sessions_collection.update_one({"thread_id": channel.id}, {"$set": {"turn_history": reconstructed_turns, "total_turns": total_count}})
            cleaned_history = []
            for t in reconstructed_turns:
                cleaned_history.append({"author": t['user_name'], "content": t['input'], "timestamp": t['timestamp'], "turn_id": t['turn_id']})
                cleaned_history.append({"author": "DM", "content": t['output'], "timestamp": t['timestamp'], "turn_id": t['turn_id']})
            await self.memory_manager.clear_thread_vectors(channel.id)
            await self.memory_manager.batch_ingest_history(channel.id, cleaned_history)
            await status_msg.edit(content=f"🔄 **Syncing...** [3/4] 🌍 Rebuilding World State (Non-Destructive)...")
            scan_tasks = []
            chunk_size = 15000 
            current_chunk = ""
            for item in cleaned_history:
                line = f"[{item['author']}]: {item['content']}\n"
                if len(current_chunk) + len(line) > chunk_size:
                    scan_tasks.append(current_chunk)
                    current_chunk = ""
                current_chunk += line
            if current_chunk: scan_tasks.append(current_chunk)
            total_chunks = len(scan_tasks)
            for i, text_chunk in enumerate(scan_tasks):
                await status_msg.edit(content=f"🔄 **Syncing...** [3/4] 🌍 Analyzing Segment {i+1}/{total_chunks}...")
                await self._run_scribe(channel.id, text_chunk)
            return total_count, total_chunks
        except Exception as e:
            RPGLogger.log(channel.id, "error", f"SYNC ERROR: {e}")
            raise e

    async def _safe_generate(self, session, content):
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                if attempt == 0: return await session.send_message_async(content)
                return await session.send_message_async("SYSTEM: Previous call MALFORMED. Retry.")
            except Exception as e:
                if "MALFORMED" in str(e) and attempt < max_retries: continue
                raise e

    async def _execute_tool(self, channel, fn, story_mode, is_reroll, session_data):
        try:
            if fn.name == "roll_d20":
                if story_mode: return "Dice disabled."
                if is_reroll and session_data.get('last_roll_result'): return f"LOCKED: {session_data['last_roll_result']}"
                diff = int(fn.args.get("difficulty", 10))
                mod = int(fn.args.get("modifier", 0))
                roll = random.randint(1, 20)
                total = roll + mod
                success = total >= diff
                desc = f"🎲 **{roll}** (d20) {f'+ {mod}' if mod >= 0 else f'- {abs(mod)}'} = **{total}** vs DC {diff}"
                color = discord.Color.green() if success else discord.Color.red()
                if roll == 20: color = discord.Color.gold(); desc += " **(CRIT!)**"
                await channel.send(embed=discord.Embed(title=f"🎲 {fn.args.get('check_type', 'Check')}", description=desc, color=color))
                res = f"Roll: {roll}, Total: {total}, DC: {diff}, Success: {success}"
                if not is_reroll: session_data['last_roll_result'] = res
                return res

            args = dict(fn.args)
            if fn.name == "update_world_entity":
                args.pop('thread_id', None)
                if 'category' not in args: return "Error: Missing 'category' argument for entity update."
                if 'name' not in args: return "Error: Missing 'name' argument for entity update."
                if "age" in args: args["age"] = sanitize_age(args["age"])
                result = tools.update_world_entity(str(channel.id), **args)
                if "Updated" in result and "(Key:" in result: result += " (NOTE: Use this canonical name in the narrative)."
                return result
            
            if fn.name == "grant_item_to_player": return tools.grant_item_to_player(**args)
            if fn.name == "apply_damage": return "Story Mode" if story_mode else tools.apply_damage(str(channel.id), **args)
            if fn.name == "apply_healing": return "Story Mode" if story_mode else tools.apply_healing(str(channel.id), **args)
            if fn.name == "deduct_mana": return "Story Mode" if story_mode else tools.deduct_mana(str(channel.id), **args)
            if fn.name == "update_journal": return tools.update_journal(str(channel.id), **args)
            if fn.name == "update_environment": return tools.update_environment(str(channel.id), **args)
            if fn.name == "manage_story_log":
                args.pop('thread_id', None)
                return tools.manage_story_log(str(channel.id), **args)
            return f"Error: Unknown tool {fn.name}"
        except Exception as e:
            return f"Tool Error: {e}"

    async def _send_narrative(self, channel, text, session, turn_id):
        # --- CLEANUP: REMOVE EXCESSIVE BREAKS ---
        # Replace 3 or more newlines with 2 (Standard Paragraph spacing)
        clean_text = re.sub(r'\n{3,}', '\n\n', text)
        # ----------------------------------------

        footer = await self.memory_manager.get_token_count_and_footer(session, turn_id)
        chunks = [clean_text[i:i+4000] for i in range(0, len(clean_text), 4000)] or ["..."]
        msg_ids = []
        
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            embed = discord.Embed(description=chunk, color=discord.Color.from_rgb(47, 49, 54))
            if i == 0: embed.set_author(name="The Dungeon Master", icon_url=self.bot.user.avatar.url)
            
            view = RPGGameView(self.bot.get_cog("RPGAdventureCog"), channel.id) if is_last else None
            if is_last: embed.set_footer(text=footer)
            
            msg = await channel.send(embed=embed, view=view)
            msg_ids.append(msg.id)
        return msg_ids

    async def _run_scribe(self, thread_id, text, active_npcs=None):
        if thread_id not in self.scribe_locks:
            self.scribe_locks[thread_id] = asyncio.Lock()
            
        async with self.scribe_locks[thread_id]:
            try:
                world_data = rpg_world_state_collection.find_one({"thread_id": int(thread_id)}) or {}
                existing = list(world_data.get("npcs", {}).keys()) + list(world_data.get("locations", {}).keys())
                known_str = ", ".join(existing) if existing else "None."
                
                active_str = ", ".join(active_npcs) if active_npcs else "Unknown (Infer from text)"

                scribe_chat = self.scribe_model.start_chat(history=[])
                prompt = prompts.SCRIBE_ANALYSIS.format(
                    narrative_text=text[:4000], 
                    known_entities=known_str,
                    active_participants=active_str
                )
                
                response = await scribe_chat.send_message_async(prompt)
                
                if response.parts:
                    for part in response.parts:
                        if part.function_call:
                            fn = part.function_call
                            if fn.name == "update_world_entity":
                                args = dict(fn.args)
                                args.pop('thread_id', None)
                                if 'category' not in args or 'name' not in args: continue 
                                if "age" in args: args["age"] = sanitize_age(args["age"])
                                tools.update_world_entity(str(thread_id), **args)
                            
                            elif fn.name == "manage_story_log":
                                args = dict(fn.args)
                                args.pop('thread_id', None)
                                tools.manage_story_log(str(thread_id), **args)
                                
            except Exception as e:
                RPGLogger.log(thread_id, "error", f"Scribe Error: {e}")

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

        RPGLogger.log(thread.id, "system", f"ADVENTURE CREATED: {title}", details={"scenario": scenario_name})

        mechanics = "2. **Story Mode Active:** NO DICE." if story_mode else "2. **Standard Mode:** Use `roll_d20` for risks."
        sys_prompt = prompts.ADVENTURE_START.format(scenario_name=scenario_name, lore=lore, mechanics=mechanics)
        
        await self.initialize_session(thread.id, session_data, "Start")
        await self.process_turn(thread, sys_prompt)