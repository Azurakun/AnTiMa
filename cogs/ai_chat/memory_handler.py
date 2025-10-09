# cogs/ai_chat/memory_handler.py
import logging
from datetime import datetime
from utils.db import ai_personal_memories_collection, ai_global_memories_collection
from .utils import _safe_get_response_text

logger = logging.getLogger(__name__)
MAX_USER_MEMORIES = 20
MAX_GLOBAL_MEMORIES_IN_PROMPT = 5

# UPDATED: Now queries based on both user and guild ID.
async def load_user_memories(user_id: int, guild_id: int) -> str:
    memories_cursor = ai_personal_memories_collection.find({"user_id": user_id, "guild_id": guild_id}).sort("timestamp", 1)
    memories = list(memories_cursor)
    if not memories: return ""
    return "\n".join([f"- {mem['summary']}" for mem in memories])

# NEW: Function to load a random sample of global memories.
async def load_global_memories() -> str:
    try:
        pipeline = [{"$sample": {"size": MAX_GLOBAL_MEMORIES_IN_PROMPT}}]
        memories_cursor = ai_global_memories_collection.aggregate(pipeline)
        memories = list(memories_cursor)
        if not memories: return ""
        return "\n".join([f"- {mem['fact']}" for mem in memories])
    except Exception:
        return ""

# REWRITTEN: This function now classifies and saves memories to the correct collection.
async def summarize_and_save_memory(summarizer_model, author, guild_id, history: list):
    if len(history) < 2: return
    
    transcript_parts = [f"{author.display_name if item.role == 'user' else 'AnTiMa'}: {item.parts[0].text if item.parts else ''}" for item in history]
    transcript = "\n".join(transcript_parts)
    
    prompt = (
        f"Analyze the following conversation transcript involving '{author.display_name}'. Your task is to extract two types of information:\n"
        "1. **Personal Memory**: Information specific to the user's personality, preferences, opinions, or personal life. Frame this from your (AnTiMa's) perspective, e.g., 'I remember {author.display_name} told me they love hiking.'\n"
        "2. **Global Fact**: Objective, verifiable information that is not personal and could be useful in any context or server. Frame this as a neutral statement, e.g., 'The speed of light is approximately 299,792 kilometers per second.'\n\n"
        "If you find a personal memory, format it as: `PERSONAL_MEMORY: [The memory from your perspective]`\n"
        "If you find a global fact, format it as: `GLOBAL_FACT: [The objective fact]`\n"
        "If you find both, provide each on a new line. If you find nothing noteworthy, output `NONE`.\n\n"
        f"--- TRANSCRIPT ---\n{transcript}\n---\n\nANALYSIS:"
    )

    try:
        response = await summarizer_model.generate_content_async(prompt)
        analysis_text = _safe_get_response_text(response)

        if "NONE" in analysis_text:
            return

        for line in analysis_text.splitlines():
            if line.startswith("PERSONAL_MEMORY:"):
                summary = line.replace("PERSONAL_MEMORY:", "").strip()
                if summary:
                    _save_personal_memory(author.id, author.name, guild_id, summary)
            elif line.startswith("GLOBAL_FACT:"):
                fact = line.replace("GLOBAL_FACT:", "").strip()
                if fact:
                    _save_global_fact(fact)

    except Exception as e:
        logger.error(f"Failed to summarize and save memory for user {author.id}: {e}")

def _save_personal_memory(user_id, user_name, guild_id, summary):
    new_memory = {
        "user_id": user_id, 
        "user_name": user_name, 
        "guild_id": guild_id, 
        "summary": summary, 
        "timestamp": datetime.utcnow()
    }
    ai_personal_memories_collection.insert_one(new_memory)
    logger.info(f"Saved new personal memory for user {user_name} in guild {guild_id}.")
    
    if ai_personal_memories_collection.count_documents({"user_id": user_id, "guild_id": guild_id}) > MAX_USER_MEMORIES:
        oldest_memory = ai_personal_memories_collection.find_one_and_delete(
            {"user_id": user_id, "guild_id": guild_id},
            sort=[("timestamp", 1)]
        )
        if oldest_memory:
            logger.info(f"Pruned oldest personal memory for user {user_name}.")

def _save_global_fact(fact):
    if not ai_global_memories_collection.find_one({"fact": fact}):
        new_fact = {"fact": fact, "timestamp": datetime.utcnow()}
        ai_global_memories_collection.insert_one(new_fact)
        logger.info(f"Saved new global fact: '{fact[:50]}...'")