# cogs/rpg_system/engine.py
import discord
import asyncio
import random
import traceback
import json
import re
from datetime import datetime, timezone

from utils.db import (
    rpg_sessions_collection, rpg_world_state_collection,
    ai_config_collection, rpg_vector_memory_collection
)
from utils.ai_client import get_client, MAIN_MODEL, FAST_MODEL, throttled_create

from .config import RPG_CLASSES
from . import prompts, tools
from .utils import RPGLogger, StatusManager, sanitize_age
from .ui import RPGGameView, DynamicActionView

# ---------------------------------------------------------------------------
# RPG Tool Schemas (OpenAI JSON format)
# ---------------------------------------------------------------------------

RPG_MAIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "grant_item_to_player",
            "description": "Adds an item to the player's permanent inventory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The player's Discord user ID."},
                    "item_name": {"type": "string", "description": "The name of the item to grant."},
                    "description": {"type": "string", "description": "A brief description of the item."}
                },
                "required": ["user_id", "item_name", "description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "apply_damage",
            "description": "Applies damage to a player, reducing their HP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "user_id": {"type": "string"},
                    "damage_amount": {"type": "integer", "description": "Amount of HP to remove."}
                },
                "required": ["thread_id", "user_id", "damage_amount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "apply_healing",
            "description": "Applies healing to a player, increasing their HP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "user_id": {"type": "string"},
                    "heal_amount": {"type": "integer", "description": "Amount of HP to restore."}
                },
                "required": ["thread_id", "user_id", "heal_amount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "deduct_mana",
            "description": "Deducts mana from a player for spell/ability use.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "user_id": {"type": "string"},
                    "mana_cost": {"type": "integer", "description": "Amount of MP to deduct."}
                },
                "required": ["thread_id", "user_id", "mana_cost"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "roll_d20",
            "description": "Simulates rolling a 20-sided die for skill checks and combat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "check_type": {"type": "string", "description": "The type of check (e.g. 'Stealth', 'Perception')."},
                    "difficulty": {"type": "integer", "description": "The difficulty class (DC) to beat."},
                    "modifier": {"type": "integer", "description": "Modifier to add to the roll."},
                    "stat_label": {"type": "string", "description": "The stat being checked."}
                },
                "required": ["check_type", "difficulty"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_journal",
            "description": "Adds a new entry to the adventure journal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "log_entry": {"type": "string", "description": "The journal entry text."}
                },
                "required": ["thread_id", "log_entry"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_world_entity",
            "description": "Updates or creates an NPC, location, quest, or event in the world state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "category": {"type": "string", "enum": ["npc", "location", "quest", "event"], "description": "The category of the entity."},
                    "name": {"type": "string", "description": "The canonical name of the entity."},
                    "details": {"type": "string", "description": "A description of the entity."},
                    "status": {"type": "string", "description": "Current status (e.g. 'active', 'background', 'dead')."},
                    "attributes": {"type": "object", "description": "Additional key-value attributes (e.g. race, gender, location, memory_add)."},
                    "memory_add": {"type": "string", "description": "A first-person memory to add to this NPC's history."},
                    "age": {"type": "string", "description": "The entity's age (for NPCs)."}
                },
                "required": ["thread_id", "category", "name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_environment",
            "description": "Updates the world's time and weather.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "time_str": {"type": "string", "description": "Current time (e.g. '14:30')."},
                    "weather": {"type": "string", "description": "Current weather description."},
                    "minutes_passed": {"type": "integer", "description": "Minutes elapsed since last update."}
                },
                "required": ["thread_id", "time_str", "weather"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_story_log",
            "description": "Manages the ongoing story events or pending NPC actions/orders.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "action": {"type": "string", "description": "Brief title of the pending event/action."},
                    "note": {"type": "string", "description": "Detailed note about the event."},
                    "status": {"type": "string", "description": "Status: 'pending' or 'resolved'."}
                },
                "required": ["thread_id", "action", "note"]
            }
        }
    },
]

RPG_SCRIBE_TOOLS = [
    t for t in RPG_MAIN_TOOLS if t["function"]["name"] in ("update_world_entity", "manage_story_log")
]


class RPGEngine:
    def __init__(self, bot, model_unused=None, memory_manager=None, scribe_model_unused=None):
        self.bot = bot
        self.memory_manager = memory_manager
        # active_sessions now stores: {channel_id: {"messages": [...], "owner_id": int, "last_prompt": str, "active_npcs": []}}
        self.active_sessions = {}
        self.scribe_locks = {}

    async def get_or_create_session(self, channel_id, session_db, initial_prompt="Resume"):
        if channel_id in self.active_sessions:
            return self.active_sessions[channel_id]['messages']
        await self.initialize_session(channel_id, session_db, initial_prompt)
        return self.active_sessions[channel_id]['messages']

    async def initialize_session(self, channel_id, session_db, initial_prompt="Resume"):
        await RPGLogger.broadcast(channel_id, "INIT", "Booting Context Manager", {"prompt": initial_prompt})

        memory_block, debug_data = await self.memory_manager.build_context_block(
            session_db, initial_prompt, logger=RPGLogger.log
        )

        active_npcs = debug_data.get("world_entities", {}).get("active_npcs", [])

        RPGLogger.log(channel_id, "system", "Context Built", details={
            "components": list(debug_data.keys()),
            "rag_hits": debug_data.get("rag_hits_count", 0),
            "active_npcs": active_npcs
        })

        # Build the initial messages list with the system prime
        system_prime = prompts.SYSTEM_PRIME.format(memory_block=memory_block)
        messages = [
            {"role": "system", "content": system_prime},
        ]

        # Send the prime to "warm up" the context (the response is acknowledged but discarded)
        try:
            client = get_client()
            prime_resp = await throttled_create(client.chat.completions.create(
                model=MAIN_MODEL,
                messages=messages,
                tools=RPG_MAIN_TOOLS,
                tool_choice="none",  # Don't call tools on prime
                max_tokens=50,
            ))
            prime_ack = prime_resp.choices[0].message.content or "Acknowledged."
            messages.append({"role": "assistant", "content": prime_ack})
            RPGLogger.log(channel_id, "system", "System Prime Accepted.")
        except Exception as e:
            RPGLogger.log(channel_id, "error", f"System Prime Failed: {e}")
            return False

        self.active_sessions[channel_id] = {
            'messages': messages,
            'owner_id': session_db['owner_id'],
            'last_prompt': initial_prompt,
            'active_npcs': active_npcs
        }
        return True

    async def process_turn(self, channel, prompt, user=None, is_reroll=False, message_id=None):
        client = get_client()
        session_db = rpg_sessions_collection.find_one({"thread_id": channel.id})
        if not session_db:
            return

        processing_msg = await channel.send("🧠 **Reading Campaign History...**")
        status = StatusManager(processing_msg)

        try:
            await RPGLogger.broadcast(channel.id, "START_TURN", "Processing User Input", {"input": prompt})

            await self.memory_manager.archive_old_turns(channel.id, session_db)
            messages = await self.get_or_create_session(channel.id, session_db, prompt)
            session_data = self.active_sessions.get(channel.id)

            active_npcs = session_data.get('active_npcs', [])
            if active_npcs:
                await status.set(f"Recalling {', '.join(active_npcs[:2])}...")
            else:
                await status.set("Scanning World State...")

            current_turn_id = session_db.get("total_turns", 0) + 1

            # --- HUD (State Injection) ---
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

            # --- Pacing Analysis ---
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

            await status.set("Drafting Narrative...")
            await RPGLogger.broadcast(channel.id, "PROMPTING", "Sending Prompt to Model", {"length": len(full_prompt)})

            async with channel.typing():
                # Append user turn to messages
                messages.append({"role": "user", "content": full_prompt})

                # --- Tool Execution Loop ---
                turns = 0
                text_content = ""
                proposed_actions = []

                while turns < 10:
                    response = await throttled_create(client.chat.completions.create(
                        model=MAIN_MODEL,
                        messages=messages,
                        tools=RPG_MAIN_TOOLS,
                        tool_choice="auto",
                    ))
                    resp_msg = response.choices[0].message

                    # Append assistant message to history
                    messages.append(resp_msg.model_dump(exclude_none=True))

                    if not resp_msg.tool_calls:
                        text_content = resp_msg.content or ""
                        break

                    turns += 1

                    # Process tool calls
                    tool_results = []
                    for tc in resp_msg.tool_calls:
                        fn_name = tc.function.name
                        try:
                            args = json.loads(tc.function.arguments)
                        except Exception:
                            args = {}

                        if fn_name == "roll_d20": await status.set("🎲 Rolling Dice...")
                        elif fn_name == "update_world_entity": await status.set("📝 Updating World...")
                        elif fn_name == "grant_item_to_player": await status.set("🎒 Managing Inventory...")
                        else: await status.set(f"🔧 Executing {fn_name}...")

                        await RPGLogger.broadcast(channel.id, "TOOL_CALL", f"Executing {fn_name}", {"args": args})

                        res_txt = await self._execute_tool(channel, fn_name, args, story_mode, is_reroll, session_data)

                        await RPGLogger.broadcast(channel.id, "TOOL_RESULT", f"Result for {fn_name}", {"output": res_txt})

                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": str(res_txt),
                        })

                    messages.extend(tool_results)

                # Fallback if no text generated
                if not text_content and turns >= 10:
                    await status.set("⚠️ Model Error. Retrying...")
                    fallback_resp = await throttled_create(client.chat.completions.create(
                        model=MAIN_MODEL,
                        messages=messages + [{"role": "user", "content": "SYSTEM: Tool execution finished. You MUST now provide the narrative description. Do not call any more tools."}],
                    ))
                    text_content = fallback_resp.choices[0].message.content or "**[System]** Narrative generation failed."

                if not text_content:
                    text_content = "**[System]** Narrative generation failed (Empty Response)."

                await status.set("✍️ Finalizing...")
                await RPGLogger.broadcast(channel.id, "NARRATIVE_GEN", "Generating Final Response", {"length": len(text_content)})

                await status.delete()

                bot_msg_ids = await self._send_narrative(
                    channel, text_content, messages, current_turn_id,
                    proposed_actions=proposed_actions, user=user
                )

                self.memory_manager.save_turn(
                    channel.id, user.name if user else "System", prompt, text_content,
                    user_message_id=message_id, bot_message_id=bot_msg_ids, current_turn_id=current_turn_id
                )

                active_list = session_data.get('active_npcs', [])
                self.bot.loop.create_task(self._run_scribe(channel.id, text_content, active_list))

                await self.memory_manager.snapshot_world_state(channel.id, current_turn_id)

                await RPGLogger.broadcast(channel.id, "TURN_COMPLETE", "Turn Finished", {"turn_id": current_turn_id})

        except Exception as e:
            await status.delete()
            RPGLogger.log(channel.id, "error", f"CRITICAL ERROR: {e}", details={"trace": traceback.format_exc()})
            await channel.send(f"⚠️ **Game Error:** {e}")

    # --- Sync Logic ---
    async def sync_session(self, channel, status_msg):
        try:
            RPGLogger.log(channel.id, "info", "SYNC: Fetching Message History...")
            raw_messages = []

            async for msg in channel.history(limit=None, oldest_first=True):
                raw_messages.append(msg)
                if len(raw_messages) % 100 == 0:
                    try:
                        await status_msg.edit(content=f"🔄 **Syncing...** [1/4] 📥 Fetched {len(raw_messages)} messages...")
                    except Exception:
                        pass

            reconstructed_turns = []
            current_turn = None
            for msg in raw_messages:
                naive_ts = msg.created_at.astimezone(timezone.utc).replace(tzinfo=None)
                if msg.author.bot and msg.author.id == self.bot.user.id:
                    if current_turn:
                        content = msg.content or (msg.embeds[0].description if msg.embeds else "")
                        if content:
                            current_turn['bot_parts'].append(content)
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
                    current_turn = {
                        'user_name': msg.author.name, 'input': msg.content,
                        'user_id': msg.id, 'timestamp': naive_ts,
                        'bot_parts': [], 'bot_msg_ids': []
                    }

            if current_turn and current_turn['bot_parts']:
                full_output = "\n".join(current_turn['bot_parts'])
                reconstructed_turns.append({
                    "timestamp": current_turn['timestamp'], "user_name": current_turn['user_name'],
                    "input": current_turn['input'], "output": full_output,
                    "user_message_id": current_turn['user_id'], "bot_message_id": current_turn['bot_msg_ids'],
                    "turn_id": len(reconstructed_turns) + 1
                })

            total_count = len(reconstructed_turns)
            await status_msg.edit(content=f"🔄 **Syncing...** [2/4] 🧩 Reconstructed {total_count} turns. Archiving...")
            rpg_sessions_collection.update_one(
                {"thread_id": channel.id},
                {"$set": {"turn_history": reconstructed_turns, "total_turns": total_count}}
            )

            cleaned_history = []
            for t in reconstructed_turns:
                cleaned_history.append({"author": t['user_name'], "content": t['input'], "timestamp": t['timestamp'], "turn_id": t['turn_id']})
                cleaned_history.append({"author": "DM", "content": t['output'], "timestamp": t['timestamp'], "turn_id": t['turn_id']})

            await self.memory_manager.clear_thread_vectors(channel.id)
            await self.memory_manager.batch_ingest_history(channel.id, cleaned_history)

            await status_msg.edit(content=f"🔄 **Syncing...** [3/4] 🌍 Rebuilding World State & Memories...")
            scan_tasks = []
            chunk_size = 8000
            current_chunk = ""
            for item in cleaned_history:
                line = f"[{item['author']}]: {item['content']}\n"
                if len(current_chunk) + len(line) > chunk_size:
                    scan_tasks.append(current_chunk)
                    current_chunk = ""
                current_chunk += line
            if current_chunk:
                scan_tasks.append(current_chunk)

            total_chunks = len(scan_tasks)
            for i, text_chunk in enumerate(scan_tasks):
                await status_msg.edit(content=f"🔄 **Syncing...** [3/4] 🌍 Analyzing Segment {i+1}/{total_chunks}...")
                await self._run_scribe(channel.id, text_chunk)
                await asyncio.sleep(2)  # Pace API calls

            await status_msg.edit(content=f"🔄 **Syncing...** [4/4] ✅ Sync Complete!")
            return total_count, total_chunks

        except Exception as e:
            RPGLogger.log(channel.id, "error", f"SYNC ERROR: {e}")
            raise e

    async def _execute_tool(self, channel, fn_name: str, args: dict, story_mode: bool, is_reroll: bool, session_data: dict) -> str:
        try:
            if fn_name == "roll_d20":
                if story_mode:
                    return "Dice disabled."
                if is_reroll and session_data.get('last_roll_result'):
                    return f"LOCKED: {session_data['last_roll_result']}"
                diff = int(args.get("difficulty", 10))
                mod = int(args.get("modifier", 0))
                roll = random.randint(1, 20)
                total = roll + mod
                success = total >= diff
                desc = f"🎲 **{roll}** (d20) {f'+ {mod}' if mod >= 0 else f'- {abs(mod)}'} = **{total}** vs DC {diff}"
                color = discord.Color.green() if success else discord.Color.red()
                if roll == 20:
                    color = discord.Color.gold()
                    desc += " **(CRIT!)**"
                await channel.send(embed=discord.Embed(
                    title=f"🎲 {args.get('check_type', 'Check')}",
                    description=desc, color=color
                ))
                res = f"Roll: {roll}, Total: {total}, DC: {diff}, Success: {success}"
                if not is_reroll:
                    session_data['last_roll_result'] = res
                return res

            if fn_name == "update_world_entity":
                args.pop('thread_id', None)
                if 'category' not in args: return "Error: Missing 'category' argument."
                if 'name' not in args: return "Error: Missing 'name' argument."
                if "age" in args: args["age"] = sanitize_age(args["age"])
                allowed_keys = {"category", "name", "details", "status", "attributes", "memory_add", "age"}
                clean_args = {k: v for k, v in args.items() if k in allowed_keys}
                result = tools.update_world_entity(str(channel.id), **clean_args)
                if "Updated" in result and "(Key:" in result:
                    result += " (NOTE: Use this canonical name in the narrative)."
                return result

            if fn_name == "grant_item_to_player": return tools.grant_item_to_player(**args)
            if fn_name == "apply_damage": return "Story Mode" if story_mode else tools.apply_damage(str(channel.id), **args)
            if fn_name == "apply_healing": return "Story Mode" if story_mode else tools.apply_healing(str(channel.id), **args)
            if fn_name == "deduct_mana": return "Story Mode" if story_mode else tools.deduct_mana(str(channel.id), **args)
            if fn_name == "update_journal": return tools.update_journal(str(channel.id), **args)
            if fn_name == "update_environment": return tools.update_environment(str(channel.id), **args)
            if fn_name == "manage_story_log":
                args.pop('thread_id', None)
                allowed_keys = {"action", "note", "status"}
                clean_args = {k: v for k, v in args.items() if k in allowed_keys}
                return tools.manage_story_log(str(channel.id), **clean_args)
            return f"Error: Unknown tool {fn_name}"
        except Exception as e:
            return f"Tool Error: {e}"

    async def _send_narrative(self, channel, text, messages, turn_id, proposed_actions=None, user=None):
        clean_text = re.sub(r'\n{3,}', '\n\n', text)
        footer = await self.memory_manager.get_token_count_and_footer(messages, turn_id)
        chunks = [clean_text[i:i+4000] for i in range(0, len(clean_text), 4000)] or ["..."]
        msg_ids = []

        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            embed = discord.Embed(description=chunk, color=discord.Color.from_rgb(47, 49, 54))
            if i == 0:
                embed.set_author(name="The Dungeon Master", icon_url=self.bot.user.avatar.url)

            view_to_send = None
            if is_last:
                session_db = rpg_sessions_collection.find_one({"thread_id": channel.id})
                ui_mode = session_db.get("ui_mode", "buttons") if session_db else "buttons"

                if ui_mode == "buttons" and proposed_actions and user:
                    action_view = DynamicActionView(self, channel, user, proposed_actions)
                    view_to_send = action_view
                else:
                    view_to_send = RPGGameView(self.bot.get_cog("RPGAdventureCog"), channel.id)

            if is_last:
                embed.set_footer(text=footer)

            msg = await channel.send(embed=embed, view=view_to_send)

            if isinstance(view_to_send, DynamicActionView):
                view_to_send.message = msg

            msg_ids.append(msg.id)
        return msg_ids

    async def _run_scribe(self, thread_id, text, active_npcs=None):
        try:
            client = get_client()
            world_data = rpg_world_state_collection.find_one({"thread_id": int(thread_id)}) or {}
            existing = list(world_data.get("npcs", {}).keys()) + list(world_data.get("locations", {}).keys())
            known_str = ", ".join(existing) if existing else "None."
            active_str = ", ".join(active_npcs) if active_npcs else "Unknown (Infer from text)"

            scribe_prompt = prompts.SCRIBE_ANALYSIS.format(
                narrative_text=text[:10000],
                known_entities=known_str,
                active_participants=active_str
            )

            response = await throttled_create(client.chat.completions.create(
                model=FAST_MODEL,
                messages=[{"role": "user", "content": scribe_prompt}],
                tools=RPG_SCRIBE_TOOLS,
                tool_choice="auto",
            ))

            resp_msg = response.choices[0].message

            if resp_msg.tool_calls:
                for tc in resp_msg.tool_calls:
                    fn_name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments)
                    except Exception:
                        args = {}

                    try:
                        if fn_name == "update_world_entity":
                            args.pop('thread_id', None)
                            if 'category' not in args or 'name' not in args:
                                continue
                            if "age" in args:
                                args["age"] = sanitize_age(args["age"])
                            allowed_keys = {"category", "name", "details", "status", "attributes", "memory_add", "age"}
                            clean_args = {k: v for k, v in args.items() if k in allowed_keys}
                            tools.update_world_entity(str(thread_id), **clean_args)

                        elif fn_name == "manage_story_log":
                            args.pop('thread_id', None)
                            allowed_keys = {"action", "note", "status"}
                            clean_args = {k: v for k, v in args.items() if k in allowed_keys}
                            tools.manage_story_log(str(thread_id), **clean_args)
                    except Exception as e:
                        RPGLogger.log(thread_id, "error", f"Scribe Tool Error: {e}")

        except Exception as e:
            error_trace = traceback.format_exc()
            print(f"CRITICAL Scribe Exception Details:\n{error_trace}")
            RPGLogger.log(thread_id, "error", f"Scribe Error: {e}")

    async def create_adventure_thread(self, interaction, lore, players, profiles, scenario_name,
                                       story_mode=False, custom_title=None, manual_guild_id=None, manual_user=None):
        client = get_client()

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

        if custom_title:
            title = custom_title
        else:
            try:
                prompt = prompts.TITLE_GENERATION.format(scenario=scenario_name, lore=lore[:100])
                resp = await throttled_create(client.chat.completions.create(
                    model=FAST_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=20,
                ))
                title = (resp.choices[0].message.content or "").strip().replace('"', '')[:50]
            except Exception:
                title = f"Quest: {owner.name}"

        thread = await channel.create_thread(
            name=title, type=discord.ChannelType.private_thread, auto_archive_duration=10080
        )
        for p in players:
            await thread.add_user(p)

        player_stats_db = {str(p.id): profiles.get(p.id, RPG_CLASSES["Freelancer"]) for p in players}

        session_data = {
            "thread_id": thread.id, "guild_id": guild_id, "owner_id": owner.id, "owner_name": owner.name,
            "title": title, "players": [p.id for p in players], "player_stats": player_stats_db,
            "scenario_type": scenario_name, "lore": lore,
            "campaign_log": [], "turn_history": [], "npc_registry": [], "quest_log": [],
            "created_at": datetime.utcnow(), "last_active": datetime.utcnow(), "active": True,
            "delete_requested": False, "story_mode": story_mode, "total_turns": 0
        }
        rpg_sessions_collection.insert_one(session_data)

        if respond:
            await respond(f"✅ Adventure **{title}** created! Check {thread.mention}")
        else:
            await channel.send(f"⚔️ **New Web-Created Adventure:** {owner.mention} begins **{title}**! -> {thread.mention}")

        RPGLogger.log(thread.id, "system", f"ADVENTURE CREATED: {title}", details={"scenario": scenario_name})

        mechanics = "2. **Story Mode Active:** NO DICE." if story_mode else "2. **Standard Mode:** Use `roll_d20` for risks."
        sys_prompt = prompts.ADVENTURE_START.format(scenario_name=scenario_name, lore=lore, mechanics=mechanics)

        await self.initialize_session(thread.id, session_data, "Start")
        await self.process_turn(thread, sys_prompt)