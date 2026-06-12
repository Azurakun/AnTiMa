# cogs/ai_chat/memory_handler.py
import logging
from datetime import datetime, timezone
from utils.db import ai_personal_memories_collection, ai_global_memories_collection

logger = logging.getLogger(__name__)

async def load_user_memories(user_id: int, guild_id: int, limit: int = 5) -> str:
    """Loads specific memories about a user in a specific guild."""
    try:
        # PyMongo is synchronous. We strictly use sync methods here.
        # We do NOT use 'await' or '.to_list()' because those are for Motor/Asyncio drivers.
        cursor = ai_personal_memories_collection.find(
            {"user_id": user_id, "guild_id": int(guild_id)}
        ).sort("timestamp", -1).limit(limit)
        
        # Convert the synchronous cursor directly to a list
        memories = list(cursor)
        
        if not memories:
            return ""
        
        # FIX: Use .get() to prevent KeyError if 'memory' field is missing in old/corrupted data
        return "\n".join([f"- {m.get('memory', '[Corrupted Memory]')}" for m in memories])
    except Exception as e:
        logger.error(f"Error loading user memories: {e}")
        return ""

async def load_global_memories(limit: int = 5) -> str:
    """Loads general/global facts the bot has learned."""
    try:
        # Synchronous find and sort
        cursor = ai_global_memories_collection.find({}).sort("timestamp", -1).limit(limit)
        
        # Convert to list immediately
        memories = list(cursor)
        
        if not memories:
            return ""
        
        # FIX: Use .get() to prevent KeyError
        return "\n".join([f"- {m.get('memory', '[Corrupted Memory]')}" for m in memories])
    except Exception as e:
        logger.error(f"Error loading global memories: {e}")
        return ""

async def summarize_and_save_memory(model, user, guild_id, conversation_history):
    """
    Analyzes conversation to extract permanent memories.
    conversation_history is a list of OpenAI-format dicts: {"role": ..., "content": ...}
    """
    try:
        from utils.ai_client import get_client, FAST_MODEL, throttled_create

        # Build a readable chat log from the last few turns
        chat_log = []
        for msg in conversation_history[-6:]:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                # Handle multi-part content (e.g. text + image)
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                content = " ".join(text_parts)
            if role and content:
                label = "AI" if role == "assistant" else "User"
                chat_log.append(f"{label}: {str(content)[:200]}")

        if not chat_log:
            return

        chat_text = "\n".join(chat_log)
        prompt = (
            "Analyze this short conversation snippet. Extract ONE specific, permanent fact about the User "
            "that is worth remembering (e.g., name, hobbies, favorite games, location). "
            "Also extract ONE general world fact if mentioned (e.g., 'The update comes out on Friday').\n"
            "If nothing new or important is mentioned, output 'None'.\n\n"
            f"Conversation:\n{chat_text}\n\n"
            "Format:\nUser Fact: [fact or None]\nGlobal Fact: [fact or None]"
        )

        client = get_client()
        response = await throttled_create(client.chat.completions.create(
            model=FAST_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
        ))
        text = (response.choices[0].message.content or "").strip()

        user_fact = None
        global_fact = None
        for line in text.split("\n"):
            if line.startswith("User Fact:") and "None" not in line:
                user_fact = line.replace("User Fact:", "").strip()
            elif line.startswith("Global Fact:") and "None" not in line:
                global_fact = line.replace("Global Fact:", "").strip()

        if user_fact:
            ai_personal_memories_collection.insert_one({
                "user_id": user.id,
                "guild_id": int(guild_id),
                "memory": user_fact,
                "timestamp": datetime.now(timezone.utc)
            })
            logger.info(f"Saved new personal memory for user {user.name} in guild {guild_id}.")

        if global_fact:
            ai_global_memories_collection.insert_one({
                "memory": global_fact,
                "timestamp": datetime.now(timezone.utc)
            })
            logger.info(f"Saved new global fact: '{global_fact[:50]}...'")

    except Exception as e:
        logger.error(f"Memory processing error: {e}")