# cogs/rpg_system/memory.py
import asyncio
import math
import re
import hashlib
from datetime import datetime, timezone

from utils.db import (
    rpg_sessions_collection,
    rpg_vector_memory_collection,
    rpg_world_state_collection,
    rpg_inventory_collection
)
from utils.timezone_manager import get_local_time
from . import prompts

# ---------------------------------------------------------------------------
# Pure-Python n-gram hash embedding — zero pip dependencies, no conflicts.
# Uses character 3-grams hashed into a 256-dim space and L2-normalised.
# Cosine similarity works well for short English text (RPG memories).
# Embedding dimension: 256
# ---------------------------------------------------------------------------
EMBED_DIM = 256

def _embed_text_sync(text: str) -> list[float]:
    """Embeds text using character 3-gram hashing into a 256-dim vector."""
    text = text.lower()[:2000]
    vec = [0.0] * EMBED_DIM
    for i in range(len(text) - 2):
        gram = text[i:i+3]
        h = int(hashlib.md5(gram.encode()).hexdigest(), 16) % EMBED_DIM
        vec[h] += 1.0
    # L2 normalise
    magnitude = math.sqrt(sum(x * x for x in vec))
    if magnitude > 0:
        vec = [x / magnitude for x in vec]
    return vec


class RPGContextManager:
    def __init__(self, model_unused=None):
        # model parameter kept for API compatibility — no longer used
        self.model = None
        self.max_tokens = 1_000_000
        self.embed_dim = EMBED_DIM
        self.HISTORY_TOKEN_BUDGET = 2500

    def _cosine_similarity(self, v1, v2):
        if not v1 or not v2 or len(v1) != len(v2):
            return 0.0
        dot_product = sum(a * b for a, b in zip(v1, v2))
        magnitude1 = math.sqrt(sum(a * a for a in v1))
        magnitude2 = math.sqrt(sum(b * b for b in v2))
        if magnitude1 == 0 or magnitude2 == 0:
            return 0.0
        return dot_product / (magnitude1 * magnitude2)

    async def _get_embedding(self, text: str) -> list[float] | None:
        """Gets semantic embedding via utils.ai_client (with fallback to local n-gram)."""
        try:
            from utils.ai_client import get_embedding
            return await get_embedding(text)
        except Exception as e:
            print(f"[RPG Embed Error] {e}")
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, _embed_text_sync, text)


    async def store_memory(self, thread_id, text, metadata=None):
        vector = await self._get_embedding(text)
        if not vector:
            return
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
                metadata={"type": "historical_sync", "date": str(chunk[0].get('timestamp')), "max_turn_id": max_turn}
            )
            count += 1
        return count

    async def retrieve_relevant_memories(self, thread_id, query_text, limit=5, threshold=0.60):
        query_vector = await self._get_embedding(query_text)
        if not query_vector:
            return []

        candidates = list(rpg_vector_memory_collection.find({"thread_id": int(thread_id)}))
        results = []
        for mem in candidates:
            score = self._cosine_similarity(query_vector, mem['vector'])
            if score >= threshold:
                results.append((score, mem['text']))

        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:limit]]

    def save_turn(self, thread_id, user_name, user_input, ai_output,
                  user_message_id=None, bot_message_id=None, current_turn_id=None):
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
        if not snapshot:
            return

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

            # Dynamic Chronological Summarization to prevent character-trigram saturation
            summary = archive_text
            try:
                from utils.ai_client import get_client, FAST_MODEL, throttled_create
                client = get_client()
                summary_prompt = (
                    "Summarize the following chronological RPG turn history into a single, cohesive, highly descriptive paragraph.\n"
                    "Focus on the main player character's actions, locations visited, quests advanced, and key NPCs encountered.\n"
                    "Write it as a seamless narrative chronicle. Return ONLY the summary, no comments or chat filler.\n\n"
                    f"{archive_text}"
                )
                resp = await throttled_create(lambda: client.chat.completions.create(
                    model=FAST_MODEL,
                    messages=[{"role": "user", "content": summary_prompt}],
                    max_tokens=250,
                ))
                summary_res = (resp.choices[0].message.content or "").strip()
                if summary_res:
                    summary = f"[Chronicle Summary (Turns {to_archive[0].get('turn_id', 1)}-{max_turn})]: {summary_res}"
            except Exception as e:
                print(f"[Archive Memory Summarization Error] {e}. Falling back to raw text.")

            await self.store_memory(
                thread_id,
                summary,
                metadata={"type": "archived_history", "max_turn_id": max_turn}
            )
            rpg_sessions_collection.update_one(
                {"thread_id": int(thread_id)},
                {"$set": {"turn_history": remaining}}
            )

    async def generate_suggested_options(self, narrative_text: str) -> list[str]:
        try:
            from utils.ai_client import get_client, FAST_MODEL, throttled_create
            import json
            prompt = (
                "Based on the following RPG story scene, suggest 3 or 4 short, contextual, action-oriented choices the player can make next.\n"
                "Keep choices concise and starting with a verb (e.g., 'Examine the chest', 'Talk to the merchant', 'Draw your sword').\n"
                "Format your output as a raw JSON list of strings, e.g. [\"Action 1\", \"Action 2\", \"Action 3\"]. Return ONLY the JSON array, no markdown formatting, backticks, or comments."
            )
            client = get_client()
            sugg_msgs = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": narrative_text}
            ]
            resp = await throttled_create(lambda: client.chat.completions.create(
                model=FAST_MODEL,
                messages=sugg_msgs,
                max_tokens=150,
            ))
            content = (resp.choices[0].message.content or "").strip()
            
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            
            return json.loads(content.strip())
        except Exception as e:
            print(f"[RPG suggested actions error] {e}")
            return ["Explore the surroundings", "Inspect your inventory", "Look for danger"]

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
            if len(items) > 12:
                item_str += f" (+{len(items)-12} more)"

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
        if not data:
            return "**System:** No world data established.", {}

        debug_snapshot = {"active_quests": [], "active_locs": [], "active_npcs": [], "recalled_npcs": []}

        env = data.get("environment", {})
        env_text = f"**🕰️ TIME:** {env.get('time', '08:00')} | **Weather:** {env.get('weather', 'Clear')}\n"

        logs = data.get("story_log", [])
        active_logs = [l for l in logs if l.get("status") == "pending"]
        log_text = ""
        if active_logs:
            log_text = "**📝 PENDING ACTIONS / ORDERS:**\n" + "".join([f"> 📌 {l['note']}\n" for l in active_logs]) + "\n"

        locations = data.get("locations", {})
        active_loc_objs = [v for v in locations.values() if v.get("status") == "active"]
        active_loc_names = [l['name'].lower().strip() for l in active_loc_objs]

        loc_text = "**📍 CURRENT LOCATION:**\n"
        if active_loc_objs:
            loc_text += "".join([f"> 🏰 **{l['name']}**: {l['details']}\n" for l in active_loc_objs])
        else:
            loc_text += "Unknown / In Transit.\n"

        debug_snapshot["active_locs"] = [l['name'] for l in active_loc_objs]

        quests = data.get("quests", {})
        active_quests = [v for v in quests.values() if v.get("status") == "active"]
        quest_text = (
            "**🛡️ ACTIVE QUESTS:**\n" + "".join([f"> 🔸 **{q['name']}**: {q['details']}\n" for q in active_quests])
            if active_quests else ""
        )
        debug_snapshot["active_quests"] = [q['name'] for q in active_quests]

        npcs = data.get("npcs", {})
        visible_npcs = []
        input_lower = current_input.lower()

        for npc in npcs.values():
            attrs = npc.get("attributes", {})
            npc_loc = attrs.get("location", "").lower().strip()
            role = attrs.get("role", "").lower().strip()
            status = npc.get("status", "background").lower()
            name_lower = npc['name'].lower()

            is_companion = "companion" in role or "party" in role
            is_present = npc_loc and (npc_loc in active_loc_names)
            is_active_forced = status == "active"
            is_mentioned = name_lower in input_lower

            if (is_companion or is_present or is_active_forced or is_mentioned) and status != "dead":
                npc["_temp_score"] = 0
                if is_mentioned: npc["_temp_score"] += 30
                if is_companion: npc["_temp_score"] += 20
                if is_present: npc["_temp_score"] += 10
                if is_active_forced: npc["_temp_score"] += 1
                visible_npcs.append(npc)

        visible_npcs.sort(key=lambda x: x.get("_temp_score", 0), reverse=True)
        # Separate into active vs recalled
        active_npcs = [n for n in visible_npcs if n.get("_temp_score", 0) >= 10][:4]
        
        active_names_set = {n['name'] for n in active_npcs}
        recalled_npcs = [n for n in visible_npcs if n['name'] not in active_names_set][:3]

        npc_list = []
        for npc in active_npcs:
            attrs = npc.get("attributes", {})
            alias_str = " ".join([f"`{a}`" for a in attrs.get("aliases", [])]) if attrs.get("aliases") else ""
            rel = attrs.get("relationships") or "Neutral"
            if isinstance(rel, list): rel = ", ".join(rel)
            clothing = attrs.get("clothing", "Standard attire")
            history = attrs.get("history", [])
            history_txt = ""
            if history:
                recent_mems = history[-3:]
                history_txt = "\n>    └─ **MEMORIES:** " + " | ".join([f"[{m['type'].upper() if 'type' in m else 'MEM'}] {m['text']}" for m in recent_mems])

            npc_list.append(
                f"> 👤 **{npc['name']}** [{attrs.get('race','?')} | {attrs.get('gender','?')}] {alias_str}\n"
                f">    ├─ **STATUS:** {attrs.get('condition','Alive')} | **WEARING:** {clothing}\n"
                f">    ├─ **RELATIONSHIP:** {rel}\n"
                f">    ├─ **INFO:** {npc['details']}"
                f"{history_txt}"
            )
            debug_snapshot["active_npcs"].append(npc['name'])
            
        for npc in recalled_npcs:
            attrs = npc.get("attributes", {})
            rel = attrs.get("relationships") or "Neutral"
            if isinstance(rel, list): rel = ", ".join(rel)
            npc_list.append(f"> 👥 **{npc['name']}** (Background/Recalled): [{rel}] {npc['details']}")
            debug_snapshot["recalled_npcs"].append(npc['name'])

        active_names = [n['name'].lower() for n in active_npcs + recalled_npcs]
        for key, npc in npcs.items():
            if npc['name'].lower() not in active_names and npc['name'].lower() in input_lower:
                npc_list.append(f"> 🧠 **{npc['name']}** (Recalled Memory): {npc['details']}")
                debug_snapshot["recalled_npcs"].append(npc['name'])

        npc_text = "**👥 NPC REGISTRY (NEARBY / ACTIVE):**\n" + "\n".join(npc_list) if npc_list else "**👥 NPC REGISTRY:** No one relevant nearby."

        events = data.get("events", {})
        event_list = list(events.values())[-5:]
        event_text = (
            "**📅 KEY EVENTS (MEMORY):**\n" + "".join([f"> 🔹 {e['name']}: {e['details']}\n" for e in event_list])
            if event_list else ""
        )

        return f"{env_text}{log_text}{quest_text}\n{loc_text}\n{npc_text}\n{event_text}", debug_snapshot

    async def build_context_block(self, session_data, current_user_input, logger=None):
        thread_id = session_data['thread_id']
        owner_id = session_data.get('owner_id')
        local_time_str = get_local_time(owner_id, fmt="%Y-%m-%d %H:%M %Z") if owner_id else "Unknown Date"

        if logger: logger(thread_id, "system", "Building Memory Context...")

        lore = session_data.get("lore", "Standard Fantasy Setting")
        player_context = self._format_player_profiles(session_data)
        world_sheet, world_debug = self._format_world_sheet(thread_id, current_user_input)

        history = session_data.get("turn_history", [])
        text_log_reversed = []
        current_cost = 0

        for i in range(len(history) - 1, -1, -1):
            turn = history[i]
            turn_text = f"[{turn['user_name']}]: {turn['input']}\n[DM]: {turn['output']}\n"
            cost = len(turn_text) * 0.3

            if current_cost + cost > self.HISTORY_TOKEN_BUDGET:
                break

            is_latest = (i == len(history) - 1)
            tag = " <--- [CURRENT MOMENT]" if is_latest else ""
            entry = f"[{turn['user_name']}]: {turn['input']}\n[DM]: {turn['output']}{tag}"
            text_log_reversed.insert(0, entry)
            current_cost += cost

        recent_history = "\n\n".join(text_log_reversed)

        if logger: logger(thread_id, "system", "Retrieving Vector Memories...")
        active_loc_list = world_debug.get("active_locs", [])
        loc_str = active_loc_list[0] if active_loc_list else ""
        active_npc_list = world_debug.get("active_npcs", [])
        npc_str = " ".join(active_npc_list)
        rag_query = f"{loc_str} {npc_str} {current_user_input}"
        rag_memories = await self.retrieve_relevant_memories(thread_id, rag_query)
        memory_text = "\n".join([f"- {m}" for m in rag_memories]) if rag_memories else "No deep archives found."

        if logger and rag_memories:
            logger(thread_id, "system", f"Found {len(rag_memories)} relevant memories.")

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
            "rag_previews": [m[:50] + "..." for m in rag_memories]
        }

        return context, debug_data

    async def get_token_count_and_footer(self, messages: list, turn_id=None) -> str:
        """
        Estimates token usage from the messages list.
        Replaces genai's count_tokens_async with a character-count heuristic.
        ~4 chars per token is a reasonable approximation.
        """
        try:
            total_chars = sum(
                len(str(m.get("content", ""))) for m in messages if isinstance(m, dict)
            )
            estimated_tokens = total_chars // 4
            percent = (estimated_tokens / self.max_tokens) * 100
            turn_str = f" | 📜 Turn {turn_id}" if turn_id else ""
            return f"🧠 Mem: ~{estimated_tokens:,} ({percent:.1f}%){turn_str}"
        except Exception:
            return "🧠 Mem: Calc Error"

    def delete_last_turn(self, thread_id):
        session = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        if not session or "turn_history" not in session:
            return None
        history = session["turn_history"]
        if not history:
            return None

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
        if not session or "turn_history" not in session:
            return [], None
        full_history = session["turn_history"]

        split_index = -1
        for idx, turn in enumerate(full_history):
            if turn.get("turn_id") == target_turn_id:
                split_index = idx
                break

        if split_index == -1:
            return [], None

        new_history = full_history[:split_index + 1]
        deleted_turns = full_history[split_index + 1:]
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