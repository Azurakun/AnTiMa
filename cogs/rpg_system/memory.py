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
        # Embedding model for RAG
        self.embed_model = "models/text-embedding-004" 

    def _cosine_similarity(self, v1, v2):
        """Calculates similarity between two vectors manually."""
        dot_product = sum(a * b for a, b in zip(v1, v2))
        magnitude1 = math.sqrt(sum(a * a for a in v1))
        magnitude2 = math.sqrt(sum(b * b for b in v2))
        if magnitude1 == 0 or magnitude2 == 0: return 0.0
        return dot_product / (magnitude1 * magnitude2)

    async def _get_embedding(self, text):
        """Generates vector embedding for text using Gemini."""
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
        """Stores a text chunk into the Vector Database."""
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

    async def retrieve_relevant_memories(self, thread_id, query_text, limit=5, threshold=0.65):
        """RAG: Finds past memories relevant to the current query."""
        query_vector = await self._get_embedding(query_text)
        if not query_vector: return []

        # fetch all memories for this thread (Simple local filtering for now)
        # Note: For massive scale, use Atlas Vector Search. For single RPG threads, this is fast enough.
        candidates = list(rpg_vector_memory_collection.find({"thread_id": int(thread_id)}))
        
        results = []
        for mem in candidates:
            score = self._cosine_similarity(query_vector, mem['vector'])
            if score >= threshold:
                results.append((score, mem['text']))
        
        # Sort by relevance
        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:limit]]

    def save_turn(self, thread_id, user_name, user_input, ai_output, user_message_id=None, bot_message_id=None):
        """Saves turn to Mongo AND Vector DB."""
        entry = {
            "timestamp": datetime.utcnow(),
            "user_name": user_name,
            "input": user_input,
            "output": ai_output,
            "user_message_id": user_message_id,
            "bot_message_id": bot_message_id 
        }
        
        # 1. Update Session History (Short Term)
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$push": {"turn_history": entry}}
        )

    async def archive_old_turns(self, thread_id, session_data):
        """Moves old turns from 'turn_history' to Vector Memory to save context space."""
        history = session_data.get("turn_history", [])
        # If history > 20 turns, archive the oldest 5
        if len(history) > 20:
            to_archive = history[:5]
            remaining = history[5:]
            
            # Create a summary/chunk for the vector DB
            archive_text = ""
            for turn in to_archive:
                archive_text += f"[{turn['user_name']}]: {turn['input']}\n[DM]: {turn['output']}\n"
            
            # Store in Vector DB
            await self.store_memory(thread_id, archive_text, metadata={"type": "archived_history"})
            
            # Update Mongo (Trim the list)
            rpg_sessions_collection.update_one(
                {"thread_id": int(thread_id)},
                {"$set": {"turn_history": remaining}}
            )

    def _format_world_sheet(self, thread_id):
        """Formats the World Sheet (NPCs/Locations) into a readable string."""
        data = rpg_world_state_collection.find_one({"thread_id": int(thread_id)})
        if not data: return "No detailed world data."
        
        output = []
        
        # Format NPCs
        npcs = data.get("npcs", {})
        active_npcs = [v for v in npcs.values() if v.get("status") == "active"]
        if active_npcs:
            output.append("**PRESENT NPCs:**")
            for npc in active_npcs:
                output.append(f"- **{npc['name']}**: {npc['details']}")
        
        # Format Locations
        locs = data.get("locations", {})
        active_locs = [v for v in locs.values() if v.get("status") == "active"]
        if active_locs:
             output.append("**CURRENT LOCATION DETAILS:**")
             for loc in active_locs:
                 output.append(f"- **{loc['name']}**: {loc['details']}")

        return "\n".join(output)

    async def build_context_block(self, session_data, current_user_input):
        """
        Constructs the Master Prompt including:
        1. World Sheet (Structured Data)
        2. RAG Memories (Retrieved based on input)
        3. Recent History (Chat Log)
        """
        thread_id = session_data['thread_id']
        owner_id = session_data.get('owner_id')
        local_time_str = get_local_time(owner_id, fmt="%Y-%m-%d %H:%M %Z") if owner_id else "Unknown Date"

        # 1. Fetch relevant past memories via RAG
        rag_memories = await self.retrieve_relevant_memories(thread_id, current_user_input)
        memory_text = "\n".join([f"- {m}" for m in rag_memories]) if rag_memories else "No specific past memories triggered."

        # 2. Fetch Structured World Sheet
        world_sheet = self._format_world_sheet(thread_id)
        
        # 3. Fetch Recent History
        history = session_data.get("turn_history", [])
        text_log = []
        for turn in history:
            text_log.append(f"[{turn['user_name']}]: {turn['input']}")
            text_log.append(f"[DM]: {turn['output']}")
        recent_history = "\n\n".join(text_log)

        # 4. Campaign Log
        campaign_summary = session_data.get("campaign_log", [])
        log_text = "\n".join([f"- {item}" for item in campaign_summary[-10:]]) # Last 10 log entries

        context = (
            f"=== 🧠 SYSTEM MEMORY ===\n"
            f"**REAL WORLD TIME:** {local_time_str}\n"
            f"**SCENARIO:** {session_data.get('scenario_type', 'Unknown')}\n\n"
            
            f"=== 🌍 WORLD SHEET (Active Context) ===\n"
            f"{world_sheet}\n\n"
            
            f"=== 📚 ARCHIVED MEMORIES (Retrieved via RAG) ===\n"
            f"The user's input triggered these memories from the past:\n"
            f"{memory_text}\n\n"
            
            f"=== 📝 CAMPAIGN LOG ===\n"
            f"{log_text}\n\n"
            
            f"=== 📜 RECENT DIALOGUE ===\n"
            f"{recent_history}\n"
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
        session = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        if not session or "turn_history" not in session: return
        full_history = session["turn_history"]
        if target_index < 0: target_index = 0
        if target_index >= len(full_history): return 
        new_history = full_history[:target_index]
        rpg_sessions_collection.update_one({"thread_id": int(thread_id)}, {"$set": {"turn_history": new_history}})
        return full_history[target_index:]