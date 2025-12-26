# cogs/rpg_system/memory.py
import discord
from datetime import datetime
import math
from utils.db import rpg_sessions_collection, rpg_vector_memory_collection, rpg_world_state_collection
from utils.timezone_manager import get_local_time
import google.generativeai as genai

class RPGContextManager:
    def __init__(self, model):
        self.model = model
        self.max_tokens = 1_000_000
        self.embed_model = "models/text-embedding-004" 

    def _cosine_similarity(self, v1, v2):
        dot_product = sum(a * b for a, b in zip(v1, v2))
        magnitude1 = math.sqrt(sum(a * a for a in v1))
        magnitude2 = math.sqrt(sum(b * b for b in v2))
        if magnitude1 == 0 or magnitude2 == 0: return 0.0
        return dot_product / (magnitude1 * magnitude2)

    async def _get_embedding(self, text):
        try:
            result = await genai.embed_content_async(
                model=self.embed_model,
                content=text,
                task_type="retrieval_document"
            )
            return result['embedding']
        except Exception as e:
            print(f"Embedding Error: {e}")
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

    async def purge_memories_since(self, thread_id, cutoff_timestamp):
        """
        Deletes all vector memories created AFTER the given timestamp.
        Used for rewinding the story state.
        """
        rpg_vector_memory_collection.delete_many({
            "thread_id": int(thread_id),
            "timestamp": {"$gt": cutoff_timestamp}
        })

    async def batch_ingest_history(self, thread_id, messages):
        chunk_size = 5
        chunks = [messages[i:i + chunk_size] for i in range(0, len(messages), chunk_size)]
        count = 0
        for chunk in chunks:
            chunk_text = ""
            for msg in chunk:
                chunk_text += f"[{msg['author']}]: {msg['content']}\n"
            await self.store_memory(
                thread_id, 
                chunk_text, 
                metadata={"type": "historical_sync", "date": chunk[0]['timestamp'].isoformat()}
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

    def save_turn(self, thread_id, user_name, user_input, ai_output, user_message_id=None, bot_message_id=None):
        entry = {
            "timestamp": datetime.utcnow(),
            "user_name": user_name,
            "input": user_input,
            "output": ai_output,
            "user_message_id": user_message_id,
            "bot_message_id": bot_message_id 
        }
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$push": {"turn_history": entry}}
        )

    async def archive_old_turns(self, thread_id, session_data):
        history = session_data.get("turn_history", [])
        if len(history) > 20:
            to_archive = history[:5]
            remaining = history[5:]
            archive_text = ""
            for turn in to_archive:
                archive_text += f"[{turn['user_name']}]: {turn['input']}\n[DM]: {turn['output']}\n"
            await self.store_memory(thread_id, archive_text, metadata={"type": "archived_history"})
            rpg_sessions_collection.update_one(
                {"thread_id": int(thread_id)},
                {"$set": {"turn_history": remaining}}
            )

    def _format_world_sheet(self, thread_id):
        data = rpg_world_state_collection.find_one({"thread_id": int(thread_id)})
        if not data: return "No detailed world data."
        output = []
        npcs = data.get("npcs", {})
        active_npcs = [v for v in npcs.values() if v.get("status") == "active"]
        if active_npcs:
            output.append("**👥 NPC DOSSIERS (Active):**")
            for npc in active_npcs:
                # Basic display, AI has full details in tool update
                output.append(f"- **{npc['name']}**: {npc['details']}")
        
        locs = data.get("locations", {})
        active_locs = [v for v in locs.values() if v.get("status") == "active"]
        if active_locs:
             output.append("**📍 CURRENT LOCATION:**")
             for loc in active_locs:
                 output.append(f"- **{loc['name']}**: {loc['details']}")
        return "\n".join(output)

    async def build_context_block(self, session_data, current_user_input):
        thread_id = session_data['thread_id']
        owner_id = session_data.get('owner_id')
        local_time_str = get_local_time(owner_id, fmt="%Y-%m-%d %H:%M %Z") if owner_id else "Unknown Date"
        
        rag_memories = await self.retrieve_relevant_memories(thread_id, current_user_input)
        memory_text = "\n".join([f"- {m}" for m in rag_memories]) if rag_memories else "No specific past memories triggered."
        
        world_sheet = self._format_world_sheet(thread_id)
        
        history = session_data.get("turn_history", [])
        text_log = []
        for turn in history:
            text_log.append(f"[{turn['user_name']}]: {turn['input']}")
            text_log.append(f"[DM]: {turn['output']}")
        recent_history = "\n\n".join(text_log)

        campaign_summary = session_data.get("campaign_log", [])
        log_text = "\n".join([f"- {item}" for item in campaign_summary[-10:]])

        context = (
            f"=== 🧠 SYSTEM MEMORY ===\n"
            f"**REAL WORLD TIME:** {local_time_str}\n"
            f"**SCENARIO:** {session_data.get('scenario_type', 'Unknown')}\n\n"
            f"=== 🌍 WORLD STATE (NPCs & LOCATIONS) ===\n{world_sheet}\n\n"
            f"=== 📚 ARCHIVED MEMORIES (RAG) ===\n{memory_text}\n\n"
            f"=== 📝 CAMPAIGN LOG ===\n{log_text}\n\n"
            f"=== 📜 RECENT DIALOGUE ===\n{recent_history}\n"
            f"=== MEMORY END ==="
        )
        return context

    async def get_token_count_and_footer(self, chat_session, turn_id=None):
        try:
            if not chat_session.history: return "🧠 Mem: 0%"
            count_result = await self.model.count_tokens_async(chat_session.history)
            used = count_result.total_tokens
            percent = (used / self.max_tokens) * 100
            turn_str = f" | 📜 Turn {turn_id}" if turn_id else ""
            return f"🧠 Mem: {used:,} ({percent:.1f}%){turn_str} | 💾 RAG Active"
        except: return "🧠 Mem: Calc Error"
        
    def delete_last_turn(self, thread_id):
        rpg_sessions_collection.update_one({"thread_id": int(thread_id)}, {"$pop": {"turn_history": 1}})
        
    def trim_history(self, thread_id, target_index):
        """
        Trims history to target_index.
        Returns: (deleted_turns_list, timestamp_of_rewind_point)
        """
        session = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        if not session or "turn_history" not in session: return [], None
        
        full_history = session["turn_history"]
        if target_index < 0: target_index = 0
        if target_index >= len(full_history): return [], None
        
        # Simpler: target_index is the index of the last turn we KEEP.
        new_history = full_history[:target_index+1] 
        deleted_turns = full_history[target_index+1:]
        
        last_kept_turn = new_history[-1] if new_history else None
        rewind_timestamp = last_kept_turn["timestamp"] if last_kept_turn else datetime.min
        
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)}, 
            {"$set": {"turn_history": new_history}}
        )
        
        return deleted_turns, rewind_timestamp