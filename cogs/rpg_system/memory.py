# cogs/rpg_system/memory.py
import discord
from datetime import datetime, timezone
import math
import asyncio
import re
from utils.db import (
    rpg_sessions_collection, 
    rpg_vector_memory_collection, 
    rpg_world_state_collection,
    rpg_inventory_collection
)
from utils.timezone_manager import get_local_time
import google.generativeai as genai
from . import prompts

class RPGContextManager:
    def __init__(self, model):
        self.model = model
        self.max_tokens = 1_000_000
        self.embed_model = "models/text-embedding-004" 
        # Smart Context Budget (Approx 2000-2500 tokens allowed for history)
        self.HISTORY_TOKEN_BUDGET = 2500

    def _cosine_similarity(self, v1, v2):
        dot_product = sum(a * b for a, b in zip(v1, v2))
        magnitude1 = math.sqrt(sum(a * a for a in v1))
        magnitude2 = math.sqrt(sum(b * b for b in v2))
        if magnitude1 == 0 or magnitude2 == 0: return 0.0
        return dot_product / (magnitude1 * magnitude2)

    async def _get_embedding(self, text):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                clean_text = str(text)[:9000] 
                result = await genai.embed_content_async(
                    model=self.embed_model,
                    content=clean_text,
                    task_type="retrieval_document"
                )
                return result['embedding']
            except Exception as e:
                err_str = str(e)
                if "504" in err_str or "Deadline" in err_str or "503" in err_str:
                    await asyncio.sleep(2 * (attempt + 1)) 
                else:
                    return None
        return None

    async def store_memory(self, thread_id, text, metadata=None):
        vector = await self._get_embedding(text)
        if not vector: return
        doc = {
            "thread_id": int(thread_id),
            "text": text,
            "vector": vector,
            "timestamp": datetime.utcnow(),
            "metadata": metadata or {}
        }
        rpg_vector_memory_collection.insert_one(doc)

    async def clear_thread_vectors(self, thread_id):
        rpg_vector_memory_collection.delete_many({"thread_id": int(thread_id)})

    async def purge_memories(self, thread_id, cutoff_timestamp, from_turn_id=None):
        query = {"thread_id": int(thread_id)}
        conditions = []

        if cutoff_timestamp:
            if cutoff_timestamp.tzinfo is not None:
                cutoff_timestamp = cutoff_timestamp.astimezone(timezone.utc).replace(tzinfo=None)
            conditions.append({"timestamp": {"$gt": cutoff_timestamp}})
        
        if from_turn_id:
            conditions.append({"metadata.max_turn_id": {"$gt": int(from_turn_id)}})

        if conditions:
            query["$or"] = conditions
            rpg_vector_memory_collection.delete_many(query)

    async def purge_memories_since(self, thread_id, cutoff_timestamp):
        await self.purge_memories(thread_id, cutoff_timestamp)

    async def batch_ingest_history(self, thread_id, messages):
        chunk_size = 5
        chunks = [messages[i:i + chunk_size] for i in range(0, len(messages), chunk_size)]
        count = 0
        for chunk in chunks:
            chunk_text = ""
            max_turn = 0
            for msg in chunk:
                chunk_text += f"[{msg['author']}]: {msg['content']}\n"
                if msg.get('turn_id', 0) > max_turn:
                    max_turn = msg.get('turn_id')

            await self.store_memory(
                thread_id, 
                chunk_text, 
                metadata={
                    "type": "historical_sync", 
                    "date": str(chunk[0].get('timestamp')), 
                    "max_turn_id": max_turn
                }
            )
            count += 1
        return count

    async def retrieve_relevant_memories(self, thread_id, query_text, limit=5, threshold=0.60):
        query_vector = await self._get_embedding(query_text)
        if not query_vector: return []

        candidates = list(rpg_vector_memory_collection.find({"thread_id": int(thread_id)}))
        results = []
        for mem in candidates:
            score = self._cosine_similarity(query_vector, mem['vector'])
            if score >= threshold:
                results.append((score, mem['text']))
        
        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:limit]]

    def save_turn(self, thread_id, user_name, user_input, ai_output, user_message_id=None, bot_message_id=None, current_turn_id=None):
        entry = {
            "timestamp": datetime.utcnow(),
            "user_name": user_name,
            "input": user_input,
            "output": ai_output,
            "user_message_id": user_message_id,
            "bot_message_id": bot_message_id,
            "turn_id": current_turn_id
        }
        
        update_op = {"$push": {"turn_history": entry}}
        if current_turn_id is not None:
             update_op["$set"] = {"total_turns": current_turn_id}

        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)},
            update_op
        )

    async def snapshot_world_state(self, thread_id, turn_id):
        world_data = rpg_world_state_collection.find_one({"thread_id": int(thread_id)})
        snapshot = {k: v for k, v in world_data.items() if k != "_id"} if world_data else {}
        
        session = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        inventory_snapshot = {}
        if session:
            for player_id in session.get("players", []):
                inv = rpg_inventory_collection.find_one({"user_id": player_id})
                if inv:
                    inventory_snapshot[str(player_id)] = inv.get("items", [])

        snapshot["_inventory_backup"] = inventory_snapshot

        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id), "turn_history.turn_id": turn_id},
            {"$set": {"turn_history.$.world_snapshot": snapshot}}
        )

    def restore_world_state(self, thread_id, snapshot):
        if not snapshot: return

        inventory_data = snapshot.pop("_inventory_backup", None)
        if inventory_data:
            for user_id_str, items in inventory_data.items():
                rpg_inventory_collection.update_one(
                    {"user_id": int(user_id_str)},
                    {"$set": {"items": items}},
                    upsert=True
                )

        snapshot["thread_id"] = int(thread_id)
        rpg_world_state_collection.replace_one(
            {"thread_id": int(thread_id)},
            snapshot,
            upsert=True
        )

    async def archive_old_turns(self, thread_id, session_data):
        history = session_data.get("turn_history", [])
        if len(history) > 40:
            to_archive = history[:5]
            remaining = history[5:]
            
            max_turn = to_archive[-1].get('turn_id', 0)
            
            archive_text = ""
            for turn in to_archive:
                archive_text += f"[{turn['user_name']}]: {turn['input']}\n[DM]: {turn['output']}\n"
            
            await self.store_memory(
                thread_id, 
                archive_text, 
                metadata={
                    "type": "archived_history",
                    "max_turn_id": max_turn
                }
            )
            rpg_sessions_collection.update_one(
                {"thread_id": int(thread_id)},
                {"$set": {"turn_history": remaining}}
            )

    def _format_player_profiles(self, session_data):
        profiles = session_data.get("player_stats", {})
        output = []
        for user_id, stats in profiles.items():
            name = stats.get("name", "Unknown Hero")
            p_class = stats.get("class", "Freelancer")
            pronouns = stats.get("pronouns", "They/They")
            backstory = stats.get("backstory", "No history known.")
            appearance = stats.get("appearance", "Standard adventurer gear.")
            personality = stats.get("personality", "Determined.")
            
            inv_data = rpg_inventory_collection.find_one({"user_id": int(user_id)})
            items = [i['name'] for i in inv_data.get('items', [])] if inv_data else ["Empty"]
            item_str = ", ".join(items[:12]) 
            if len(items) > 12: item_str += f" (+{len(items)-12} more)"
            
            profile_txt = (
                f"👤 **{name}** ({p_class}) [{pronouns}]\n"
                f"   - **HP:** {stats.get('hp', 100)}/{stats.get('max_hp', 100)} | **MP:** {stats.get('mp', 50)}/{stats.get('max_mp', 50)}\n"
                f"   - **Inventory:** {item_str}\n"
                f"   - **App:** {appearance}\n"
                f"   - **Personality:** {personality}\n"
                f"   - **Backstory:** {backstory}"
            )
            output.append(profile_txt)
        return "\n".join(output)

    def _format_world_sheet(self, thread_id, current_input=""):
        data = rpg_world_state_collection.find_one({"thread_id": int(thread_id)})
        if not data: return "**System:** No world data established.", {}
        
        debug_snapshot = {"active_quests": [], "active_locs": [], "active_npcs": [], "recalled_npcs": []}

        # 0. ENVIRONMENT
        env = data.get("environment", {})
        time_str = env.get("time", "08:00")
        weather_str = env.get("weather", "Clear")
        env_text = f"**🕰️ TIME:** {time_str} | **Weather:** {weather_str}\n"

        # 1. LOGS
        logs = data.get("story_log", [])
        active_logs = [l for l in logs if l.get("status") == "pending"]
        log_text = ""
        if active_logs:
            log_text = "**📝 PENDING ACTIONS / ORDERS:**\n" + "".join([f"> 📌 {l['note']}\n" for l in active_logs]) + "\n"

        # 2. LOCATIONS
        locations = data.get("locations", {})
        active_loc_objs = [v for v in locations.values() if v.get("status") == "active"]
        active_loc_names = [l['name'].lower().strip() for l in active_loc_objs]
        
        loc_text = "**📍 CURRENT LOCATION:**\n" 
        if active_loc_objs:
            loc_text += "".join([f"> 🏰 **{l['name']}**: {l['details']}\n" for l in active_loc_objs])
        else:
            loc_text += "Unknown / In Transit.\n"
        
        debug_snapshot["active_locs"] = [l['name'] for l in active_loc_objs]

        # 3. QUESTS
        quests = data.get("quests", {})
        active_quests = [v for v in quests.values() if v.get("status") == "active"]
        quest_text = "**🛡️ ACTIVE QUESTS:**\n" + "".join([f"> 🔸 **{q['name']}**: {q['details']}\n" for q in active_quests]) if active_quests else ""
        debug_snapshot["active_quests"] = [q['name'] for q in active_quests]

        # 4. NPC REGISTRY (OPTIMIZED WITH AUTO-CULL)
        npcs = data.get("npcs", {})
        visible_npcs = []
        input_lower = current_input.lower()

        # Phase 1: Gather potential candidates with detailed scoring
        for npc in npcs.values():
            attrs = npc.get("attributes", {})
            npc_loc = attrs.get("location", "").lower().strip()
            role = attrs.get("role", "").lower().strip()
            status = npc.get("status", "background").lower()
            name_lower = npc['name'].lower()

            # PRIORITY FLAGS
            is_companion = "companion" in role or "party" in role
            is_present = npc_loc and (npc_loc in active_loc_names)
            is_active_forced = status == "active"
            is_mentioned = name_lower in input_lower

            # Base Inclusion Check
            if (is_companion or is_present or is_active_forced or is_mentioned) and status != "dead":
                # Assign Priority Score (Higher = More likely to stay if bloat occurs)
                npc["_temp_score"] = 0 
                if is_mentioned: npc["_temp_score"] += 30  # Highest priority: User is talking about them
                if is_companion: npc["_temp_score"] += 20  # High priority: Party member
                if is_present: npc["_temp_score"] += 10    # Medium priority: In the room
                if is_active_forced: npc["_temp_score"] += 1 # Low priority: Just marked active in DB
                
                visible_npcs.append(npc)
        
        # Phase 2: Safety Cap (Anti-Bloat)
        # If we have > 15 Active NPCs, we are likely experiencing "Stuck" issues.
        # We auto-cull the list to the top 15 most relevant ones.
        if len(visible_npcs) > 15:
            # Sort by score descending (Mentioned > Companion > Present > Active)
            visible_npcs.sort(key=lambda x: x.get("_temp_score", 0), reverse=True)
            visible_npcs = visible_npcs[:15] # Hard Cap

        # Format Final List
        npc_list = []
        for npc in visible_npcs:
            details = npc['details']
            attrs = npc.get("attributes", {})
            alias_str = " ".join([f"`{a}`" for a in attrs.get("aliases", [])]) if attrs.get("aliases") else ""
            rel = attrs.get("relationships") or "Neutral"
            if isinstance(rel, list): rel = ", ".join(rel)
            
            clothing = attrs.get("clothing", "Standard attire")
            
            history = attrs.get("history", [])
            history_txt = ""
            if history:
                recent_mems = history[-3:] 
                history_txt = "\n>    └─ **MEMORIES:** " + " | ".join([f"[{m['type'].upper()}] {m['text']}" for m in recent_mems])

            npc_list.append(
                f"> 👤 **{npc['name']}** [{attrs.get('race','?')} | {attrs.get('gender','?')}] {alias_str}\n"
                f">    ├─ **STATUS:** {attrs.get('condition','Alive')} | **WEARING:** {clothing}\n"
                f">    ├─ **RELATIONSHIP:** {rel}\n"
                f">    ├─ **INFO:** {details}"
                f"{history_txt}"
            )
            debug_snapshot["active_npcs"].append(npc['name'])
        
        # Recalled NPCs (Explicit mentions of people NOT active)
        # (This is mostly redundant now due to is_mentioned logic above, but kept for deep background recalls)
        active_names = [n['name'].lower() for n in visible_npcs]
        for key, npc in npcs.items():
            if npc['name'].lower() not in active_names and npc['name'].lower() in input_lower:
                # Add if not already included in the active list
                npc_list.append(f"> 🧠 **{npc['name']}** (Recalled Memory): {npc['details']}")
                debug_snapshot["recalled_npcs"].append(npc['name'])
        
        npc_text = "**👥 NPC REGISTRY (NEARBY / ACTIVE):**\n" + "\n".join(npc_list) if npc_list else "**👥 NPC REGISTRY:** No one relevant nearby."

        # 5. EVENTS
        events = data.get("events", {})
        event_list = list(events.values())[-5:] 
        event_text = "**📅 KEY EVENTS (MEMORY):**\n" + "".join([f"> 🔹 {e['name']}: {e['details']}\n" for e in event_list]) if event_list else ""

        return f"{env_text}{log_text}{quest_text}\n{loc_text}\n{npc_text}\n{event_text}", debug_snapshot

    async def build_context_block(self, session_data, current_user_input, logger=None):
        thread_id = session_data['thread_id']
        owner_id = session_data.get('owner_id')
        local_time_str = get_local_time(owner_id, fmt="%Y-%m-%d %H:%M %Z") if owner_id else "Unknown Date"
        
        if logger: logger(thread_id, "system", "Building Memory Context...")

        lore = session_data.get("lore", "Standard Fantasy Setting")
        player_context = self._format_player_profiles(session_data)
        world_sheet, world_debug = self._format_world_sheet(thread_id, current_user_input)
        
        # --- SMART CONTEXT PRUNING (Token Budgeting) ---
        history = session_data.get("turn_history", [])
        text_log_reversed = []
        current_cost = 0
        
        # Walk backwards: Add newest turns first
        for i in range(len(history) - 1, -1, -1):
            turn = history[i]
            # Rough estimation: 1 character ~= 0.3 tokens
            turn_text = f"[{turn['user_name']}]: {turn['input']}\n[DM]: {turn['output']}\n"
            cost = len(turn_text) * 0.3
            
            if current_cost + cost > self.HISTORY_TOKEN_BUDGET:
                break
                
            is_latest = (i == len(history) - 1)
            tag = " <--- [CURRENT MOMENT]" if is_latest else ""
            
            # Prepend because we are iterating backwards
            entry = f"[{turn['user_name']}]: {turn['input']}\n[DM]: {turn['output']}{tag}"
            text_log_reversed.insert(0, entry) 
            current_cost += cost
            
        recent_history = "\n\n".join(text_log_reversed)
        # -----------------------------------------------

        if logger: logger(thread_id, "system", "Retrieving Vector Memories...")
        rag_memories = await self.retrieve_relevant_memories(thread_id, current_user_input)
        memory_text = "\n".join([f"- {m}" for m in rag_memories]) if rag_memories else "No deep archives found."
        
        if logger and rag_memories: logger(thread_id, "system", f"Found {len(rag_memories)} relevant memories.")

        def escape(s):
            return str(s).replace("{", "{{").replace("}", "}}")

        context = prompts.CONTEXT_BLOCK.format(
            time=escape(local_time_str),
            scenario=escape(session_data.get('scenario_type', 'Unknown')),
            lore=escape(lore),
            player_context=escape(player_context),
            world_sheet=escape(world_sheet),
            recent_history=escape(recent_history),
            memory_text=escape(memory_text)
        )
        
        debug_data = {
            "world_entities": world_debug,
            "rag_hits_count": len(rag_memories),
            "rag_previews": [m[:50]+"..." for m in rag_memories]
        }
        
        return context, debug_data
    
    async def get_token_count_and_footer(self, chat_session, turn_id=None):
        try:
            if not chat_session.history: return "🧠 Mem: 0%"
            count_result = await self.model.count_tokens_async(chat_session.history)
            used = count_result.total_tokens
            percent = (used / self.max_tokens) * 100
            turn_str = f" | 📜 Turn {turn_id}" if turn_id else ""
            return f"🧠 Mem: {used:,} ({percent:.1f}%){turn_str}"
        except: return "🧠 Mem: Calc Error"
        
    def delete_last_turn(self, thread_id):
        session = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        if not session or "turn_history" not in session: return None
        history = session["turn_history"]
        if not history: return None
        
        deleted_turn = history.pop()
        
        rpg_sessions_collection.update_one({"thread_id": int(thread_id)}, {
            "$pop": {"turn_history": 1},
            "$inc": {"total_turns": -1}
        })
        
        new_last_turn = history[-1] if history else None
        
        if new_last_turn and "world_snapshot" in new_last_turn:
            self.restore_world_state(thread_id, new_last_turn["world_snapshot"])
        elif not new_last_turn:
             rpg_world_state_collection.update_one(
                 {"thread_id": int(thread_id)},
                 {"$set": {"quests": {}, "npcs": {}, "locations": {}, "events": {}, "environment": {}}}
             )
        
        return deleted_turn

    def trim_history(self, thread_id, target_turn_id):
        session = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        if not session or "turn_history" not in session: return [], None
        full_history = session["turn_history"]
        
        split_index = -1
        for idx, turn in enumerate(full_history):
            if turn.get("turn_id") == target_turn_id:
                split_index = idx
                break
        
        if split_index == -1: return [], None

        new_history = full_history[:split_index+1] 
        deleted_turns = full_history[split_index+1:]
        last_kept_turn = new_history[-1] if new_history else None
        
        rewind_timestamp = last_kept_turn["timestamp"] if last_kept_turn else datetime.min
        
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)}, 
            {"$set": {"turn_history": new_history, "total_turns": target_turn_id}}
        )

        if last_kept_turn and "world_snapshot" in last_kept_turn:
            self.restore_world_state(thread_id, last_kept_turn["world_snapshot"])
        elif not last_kept_turn:
            rpg_world_state_collection.update_one(
                 {"thread_id": int(thread_id)},
                 {"$set": {"quests": {}, "npcs": {}, "locations": {}, "events": {}, "environment": {}}}
             )
        
        return deleted_turns, rewind_timestamp