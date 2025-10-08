# cogs/ai_chat/memory_handler.py
import logging
from datetime import datetime
from utils.db import ai_memories_collection
from .utils import _safe_get_response_text  # <-- UPDATED IMPORT

logger = logging.getLogger(__name__)
MAX_USER_MEMORIES = 20

async def load_user_memories(user_id: int) -> str:
    memories_cursor = ai_memories_collection.find({"user_id": user_id}).sort("timestamp", 1)
    memories = list(memories_cursor)
    if not memories: return ""
    return "\n".join([f"Memory {i+1}: {mem['summary']}" for i, mem in enumerate(memories)])

async def summarize_and_save_memory(summarizer_model, author, history: list):
    if len(history) < 2: return
    transcript_parts = [f"{author.display_name if item.role == 'user' else 'AnTiMa'}: {item.parts[0].text if item.parts else ''}" for item in history]
    transcript = "\n".join(transcript_parts)
    
    prompt = (
        f"You are a memory creation AI. Your name is AnTiMa. Create a concise, first-person memory entry from your perspective "
        f"about your conversation with '{author.display_name}'. Focus on their preferences, questions, or personal details. "
        f"Frame it like you're remembering it, e.g., 'I remember talking to {author.display_name} about...'. Keep it under 150 words.\n\n"
        f"if there's a MENTION tag, replace it with the user's actual username. For example, you mention a user named 'SomeUser' with [MENTION: SomeUser], you would write 'SomeUser' on the memory."
        f"TRANSCRIPT:\n---\n{transcript}\n---\n\nMEMORY ENTRY:"
    )
    
    try:
        response = await summarizer_model.generate_content_async(prompt)
        summary = _safe_get_response_text(response)
        if not summary: return

        new_memory = {"user_id": author.id, "user_name": author.name, "summary": summary, "timestamp": datetime.utcnow()}
        ai_memories_collection.insert_one(new_memory)
        logger.info(f"Saved new memory for user {author.name} ({author.id}).")

        if ai_memories_collection.count_documents({"user_id": author.id}) > MAX_USER_MEMORIES:
            oldest_memories = ai_memories_collection.find({"user_id": author.id}, {"_id": 1}).sort("timestamp", 1).limit(1)
            ids_to_delete = [mem["_id"] for mem in oldest_memories]
            if ids_to_delete:
                ai_memories_collection.delete_many({"_id": {"$in": ids_to_delete}})
                logger.info(f"Pruned oldest memory for user {author.name}.")
    except Exception as e:
        logger.error(f"Failed to summarize and save memory for user {author.id}: {e}")