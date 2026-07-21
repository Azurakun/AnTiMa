# cogs/rpg_system/web_engine.py
import asyncio
import json
import random
import traceback
import re
from datetime import datetime, timezone

from utils.db import (
    rpg_sessions_collection, rpg_world_state_collection,
    rpg_vector_memory_collection, rpg_inventory_collection
)
from utils.ai_client import get_client, MAIN_MODEL, FAST_MODEL, throttled_create
from cogs.rpg_system.config import RPG_CLASSES
from cogs.rpg_system import prompts, tools
from cogs.rpg_system.utils import RPGLogger, sanitize_age, parse_narrative_response
from cogs.rpg_system.memory import RPGContextManager

# Reuse same tool schemas
from cogs.rpg_system.engine import RPG_MAIN_TOOLS, RPG_SCRIBE_TOOLS

class WebRPGEngine:
    def __init__(self):
        self.memory_manager = RPGContextManager()

    async def get_or_create_session(self, thread_id, session_db, initial_prompt="Resume"):
        """Ensures session is warmed up. Returns the messages list."""
        memory_block, debug_data = await self.memory_manager.build_context_block(
            session_db, initial_prompt, logger=None
        )
        system_prime = prompts.SYSTEM_PRIME.format(memory_block=memory_block)
        messages = [
            {"role": "system", "content": system_prime},
        ]
        try:
            client = get_client()
            prime_resp = await throttled_create(lambda: client.chat.completions.create(
                model=MAIN_MODEL,
                messages=messages,
                tools=RPG_MAIN_TOOLS,
                tool_choice="none",
                max_tokens=50,
            ))
            prime_ack = prime_resp.choices[0].message.content or "Acknowledged."
            messages.append({"role": "assistant", "content": prime_ack})
        except Exception as e:
            print(f"[Web RPG Engine] Prime Failed: {e}")
        return messages

    async def process_web_turn(self, thread_id: int, prompt: str, user_id: int, user_name: str, is_reroll: bool = False):
        """Processes a single turn in a web campaign, executing tools and Scribe."""
        client = get_client()
        session_db = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        if not session_db:
            raise ValueError("RPG Session not found.")

        roll_events = []
        
        # 1. Archive old turns if needed
        await self.memory_manager.archive_old_turns(int(thread_id), session_db)

        # 2. Build Context & Warmup Prime
        messages = await self.get_or_create_session(int(thread_id), session_db, prompt)

        # 3. Append historical context messages from turn history to keep the flow consistent (last 6 items)
        history = session_db.get("turn_history", [])
        for turn in history[-3:]:  # Last 3 turns (6 messages: User + DM)
            # Reconstruct the HUD part for matching formatting
            hud_hint = f"[SYSTEM STATE: Turn {turn.get('turn_id', 1)}]"
            messages.append({"role": "user", "content": f"{hud_hint}\n\n{turn['user_name']}: {turn['input']}"})
            messages.append({"role": "assistant", "content": turn['output']})

        # 4. State HUD composition
        world_data = rpg_world_state_collection.find_one({"thread_id": int(thread_id)}) or {}
        players = session_db.get("player_stats", {})
        p_data = players.get(str(user_id)) or list(players.values())[0] if players else {}
        
        hp_str = f"{p_data.get('hp', 0)}/{p_data.get('max_hp', 100)}"
        mp_str = f"{p_data.get('mp', 0)}/{p_data.get('max_mp', 50)}"

        locs = world_data.get("locations", {})
        active_loc = next((l['name'] for l in locs.values() if l.get('status') == 'active'), "Unknown")
        quests = world_data.get("quests", {})
        active_q = next((q['name'] for q in quests.values() if q.get('status') == 'active'), "None")
        env = world_data.get("environment", {})
        time_str = env.get("time", "Day")

        current_turn_id = session_db.get("total_turns", 0) + 1

        hud_update = (
            f"[SYSTEM STATE UPDATE: Turn {current_turn_id} | Time: {time_str}]\n"
            f"[LOCATION: {active_loc} | QUEST: {active_q}]\n"
            f"[STATUS: {p_data.get('name', 'Player')} - HP {hp_str} | MP {mp_str}]\n"
            "(Remind the user of these stats ONLY if relevant to the action.)"
        )

        # 5. Pacing and Social Pressure
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

        full_prompt = f"{hud_update}\n\n" + prompts.GAME_TURN.format(
            user_action=f"{user_name}: {prompt}",
            mechanics_instruction=mechanics_instr,
            pacing=pacing,
            reroll_instruction=reroll_instr + social_pressure
        )

        messages.append({"role": "user", "content": full_prompt})

        # 6. Tool Loop Execution
        turns = 0
        text_content = ""

        while turns < 10:
            response = await throttled_create(lambda: client.chat.completions.create(
                model=MAIN_MODEL,
                messages=messages,
                tools=RPG_MAIN_TOOLS,
                tool_choice="auto",
            ))
            resp_msg = response.choices[0].message
            messages.append(resp_msg.model_dump(exclude_none=True))

            if not resp_msg.tool_calls:
                text_content = resp_msg.content or ""
                break

            turns += 1
            tool_results = []
            for tc in resp_msg.tool_calls:
                fn_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except Exception:
                    args = {}

                # Execute local web-adapted tools
                res_txt = await self._execute_tool_web(thread_id, fn_name, args, story_mode, is_reroll, user_id, roll_events)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(res_txt),
                })
            messages.extend(tool_results)

        # Fallback
        if not text_content and turns >= 10:
            fallback_msgs = messages + [{"role": "user", "content": "SYSTEM: Tool execution finished. You MUST now provide the narrative description. Do not call any more tools."}]
            fallback_resp = await throttled_create(lambda: client.chat.completions.create(
                model=MAIN_MODEL,
                messages=fallback_msgs,
            ))
            text_content = fallback_resp.choices[0].message.content or "**[System]** Narrative generation failed."

        if not text_content:
            text_content = "**[System]** Narrative generation failed."

        # Extract narrative and suggested actions from structured XML response
        narrative_parsed, choices_parsed = parse_narrative_response(text_content)
        clean_text = re.sub(r'\n{3,}', '\n\n', narrative_parsed)

        # 7. Save turn & snapshot world state
        self.memory_manager.save_turn(
            int(thread_id), user_name, prompt, clean_text,
            user_message_id=None, bot_message_id=None, current_turn_id=current_turn_id
        )
        await self.memory_manager.snapshot_world_state(int(thread_id), current_turn_id)

        # 8. Run Scribe in background (no self.bot needed)
        active_list = world_data.get("npcs", {})
        active_npc_names = [n["name"] for n in active_list.values() if n.get("status") == "active"]
        player_name = p_data.get('name', 'Player')
        asyncio.create_task(self._run_scribe_web(int(thread_id), clean_text, player_name, active_npc_names))

        # 9. Extract dynamic suggested actions
        if choices_parsed:
            suggested_actions = choices_parsed
        else:
            suggested_actions = await self._generate_suggested_options(clean_text)

        # 10. Load updated stats & inventory to return to web client
        session_updated = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        world_updated = rpg_world_state_collection.find_one({"thread_id": int(thread_id)}) or {}
        inv_updated = rpg_inventory_collection.find_one({"user_id": int(user_id)})
        
        return {
            "narrative": clean_text,
            "turn_id": current_turn_id,
            "roll_events": roll_events,
            "suggested_actions": suggested_actions,
            "player_stats": session_updated.get("player_stats", {}),
            "world_state": {
                "environment": world_updated.get("environment", {}),
                "quests": list(world_updated.get("quests", {}).values()),
                "npcs": list(world_updated.get("npcs", {}).values()),
                "locations": list(world_updated.get("locations", {}).values()),
                "events": list(world_updated.get("events", {}).values()),
                "story_log": world_updated.get("story_log", [])
            },
            "inventory": inv_updated.get("items", []) if inv_updated else []
        }

    async def _execute_tool_web(self, thread_id: int, fn_name: str, args: dict, story_mode: bool, is_reroll: bool, user_id: int, roll_events: list) -> str:
        try:
            if fn_name == "roll_d20":
                if story_mode:
                    return "Dice disabled."
                diff = int(args.get("difficulty", 10))
                mod = int(args.get("modifier", 0))
                roll = random.randint(1, 20)
                total = roll + mod
                success = total >= diff
                desc = f"🎲 rolled **{roll}** {f'+ {mod}' if mod >= 0 else f'- {abs(mod)}'} = **{total}** vs DC {diff}"
                roll_events.append({
                    "check_type": args.get("check_type", "Check"),
                    "roll": roll,
                    "modifier": mod,
                    "total": total,
                    "difficulty": diff,
                    "success": success,
                    "desc": desc
                })
                return f"Roll: {roll}, Total: {total}, DC: {diff}, Success: {success}"

            if fn_name == "update_world_entity":
                args.pop('thread_id', None)
                if 'category' not in args or 'name' not in args: 
                    return "Error: Missing category or name."
                if "age" in args: 
                    args["age"] = sanitize_age(args["age"])
                allowed_keys = {"category", "name", "details", "status", "attributes", "memory_add", "age"}
                clean_args = {k: v for k, v in args.items() if k in allowed_keys}
                return tools.update_world_entity(str(thread_id), **clean_args)

            if fn_name == "grant_item_to_player": 
                # Ensure it targets the active web user
                args["user_id"] = str(user_id)
                return tools.grant_item_to_player(**args)

            if fn_name == "modify_player_stats":
                args.pop('thread_id', None)
                args["user_id"] = str(user_id)
                return "Story Mode" if story_mode else tools.modify_player_stats(str(thread_id), **args)

            if fn_name == "recall_memory":
                q = args.get("query", "")
                res = await self.memory_manager.retrieve_relevant_memories(int(thread_id), q, limit=3)
                if not res:
                    return f"Memory Recall Failed: No records found regarding '{q}'."
                return f"Memory Recall Results for '{q}':\n" + "\n".join([f"- {m}" for m in res])

            if fn_name == "update_journal": 
                return tools.update_journal(str(thread_id), **args)

            if fn_name == "update_environment": 
                return tools.update_environment(str(thread_id), **args)

            if fn_name == "manage_story_log":
                args.pop('thread_id', None)
                allowed_keys = {"action", "note", "status"}
                clean_args = {k: v for k, v in args.items() if k in allowed_keys}
                return tools.manage_story_log(str(thread_id), **clean_args)

            return f"Error: Unknown tool {fn_name}"
        except Exception as e:
            return f"Tool Error: {e}"

    async def _run_scribe_web(self, thread_id: int, text: str, player_name: str, active_npcs: list = None):
        try:
            client = get_client()
            world_data = rpg_world_state_collection.find_one({"thread_id": int(thread_id)}) or {}
            existing = list(world_data.get("npcs", {}).keys()) + list(world_data.get("locations", {}).keys())
            known_str = ", ".join(existing) if existing else "None."
            active_str = ", ".join(active_npcs) if active_npcs else "Unknown"

            scribe_prompt = prompts.SCRIBE_ANALYSIS.format(
                player_name=player_name,
                narrative_text=text[:10000],
                known_entities=known_str,
                active_participants=active_str
            )

            response = await throttled_create(lambda: client.chat.completions.create(
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
                        print(f"[Web RPG Scribe] Tool error: {e}")

        except Exception as e:
            print(f"[Web RPG Scribe] Error: {e}")

    async def _generate_suggested_options(self, narrative_text: str) -> list[str]:
        return await self.memory_manager.generate_suggested_options(narrative_text)
