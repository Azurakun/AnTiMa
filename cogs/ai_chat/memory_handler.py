# cogs/ai_chat/memory_handler.py
import logging
import asyncio
from datetime import datetime, timezone
from utils.db import (
    ai_personal_memories_collection,
    ai_global_memories_collection,
    ai_hybrid_memories_collection
)
from utils.ai_client import embed
from utils.vector_search import rank_documents

logger = logging.getLogger(__name__)


async def load_user_memories(user_id: int, guild_id: int, query_text: str = "", limit: int = 5) -> str:
    """Loads specific memories about a user in a specific guild using hybrid relational & vector search."""
    try:
        memories_set = set()
        
        # 1. Direct recent personal memories
        recent = await asyncio.to_thread(lambda: list(
            ai_personal_memories_collection.find({"user_id": user_id, "guild_id": int(guild_id)})
            .sort("timestamp", -1).limit(limit)
        ))
        for m in recent:
            mem_text = m.get("memory")
            if mem_text and mem_text != "[Corrupted Memory]":
                memories_set.add(mem_text)

        # 2. Vector hybrid memory search
        if query_text:
            candidates = await asyncio.to_thread(lambda: list(
                ai_hybrid_memories_collection.find({"user_id": user_id, "guild_id": int(guild_id)})
            ))
            ranked = await rank_documents(query_text, candidates, text_key="memory",
                                          collection=ai_hybrid_memories_collection,
                                          threshold=0.15, limit=limit)
            for _, doc in ranked:
                memories_set.add(doc["memory"])

        if not memories_set:
            return ""

        return "\n".join([f"- {m}" for m in list(memories_set)[:limit]])
    except Exception as e:
        logger.error(f"Error loading user memories: {e}")
        return ""


async def load_global_memories(limit: int = 5) -> str:
    """Loads general/global facts the bot has learned."""
    try:
        memories = await asyncio.to_thread(lambda: list(
            ai_global_memories_collection.find({}).sort("timestamp", -1).limit(limit)
        ))
        if not memories:
            return ""
        return "\n".join([f"- {m.get('memory', '[Corrupted Memory]')}" for m in memories])
    except Exception as e:
        logger.error(f"Error loading global memories: {e}")
        return ""


async def summarize_and_save_memory(model, user, guild_id, conversation_history, channel_id: int = None):
    """
    Analyzes conversation to extract permanent memories and relational facts.
    conversation_history is a list of OpenAI-format dicts: {"role": ..., "content": ...}
    """
    try:
        from utils.ai_client import get_client, FAST_MODEL, throttled_create

        chat_log = []
        for msg in conversation_history[-6:]:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                content = " ".join(text_parts)
            if role and content:
                label = "AI" if role == "assistant" else "User"
                chat_log.append(f"{label}: {str(content)[:200]}")

        if not chat_log:
            return

        chat_text = "\n".join(chat_log)
        prompt = (
            "Analyze this short conversation snippet. Extract:\n"
            "1. ONE specific, permanent fact about the User (e.g., name, hobbies, favorite games, location).\n"
            "2. ONE relational triple (Subject | Relation | Object, e.g., 'User | plays | Genshin Impact').\n"
            "3. ONE general world fact if mentioned (e.g., 'The server event is on Friday').\n"
            "If nothing new or important is mentioned for any category, output 'None'.\n\n"
            f"Conversation:\n{chat_text}\n\n"
            "Format:\nUser Fact: [fact or None]\nRelational: [triple or None]\nGlobal Fact: [fact or None]"
        )

        client = get_client()
        response = await throttled_create(lambda: client.chat.completions.create(
            model=FAST_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
        ))
        text = (response.choices[0].message.content or "").strip()

        user_fact = None
        relational_triple = None
        global_fact = None
        for line in text.split("\n"):
            if line.startswith("User Fact:") and "None" not in line:
                user_fact = line.replace("User Fact:", "").strip()
            elif line.startswith("Relational:") and "None" not in line:
                relational_triple = line.replace("Relational:", "").strip()
            elif line.startswith("Global Fact:") and "None" not in line:
                global_fact = line.replace("Global Fact:", "").strip()

        if user_fact:
            vector, embed_model = await embed(user_fact)
            await asyncio.to_thread(ai_hybrid_memories_collection.insert_one, {
                "user_id": user.id,
                "guild_id": int(guild_id),
                "channel_id": int(channel_id) if channel_id else 0,
                "memory": user_fact,
                "relational": relational_triple,
                "vector": vector,
                "embed_model": embed_model,
                "timestamp": datetime.now(timezone.utc)
            })
            await asyncio.to_thread(ai_personal_memories_collection.insert_one, {
                "user_id": user.id,
                "guild_id": int(guild_id),
                "memory": user_fact,
                "timestamp": datetime.now(timezone.utc)
            })
            logger.info(f"Saved hybrid memory for user {user.name} in guild {guild_id}.")

        if global_fact:
            await asyncio.to_thread(ai_global_memories_collection.insert_one, {
                "memory": global_fact,
                "timestamp": datetime.now(timezone.utc)
            })
            logger.info(f"Saved new global fact: '{global_fact[:50]}...'")

    except Exception as e:
        logger.error(f"Memory processing error: {e}")