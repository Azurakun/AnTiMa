# cogs/rpg_system/memory.py
import google.generativeai as genai
from utils.db import rpg_sessions_collection

class RPGMemoryManager:
    def __init__(self, model):
        self.model = model
        self.max_tokens = 1_000_000  # 1 Million Token Limit

    async def load_history(self, thread_id):
        """
        Retrieves the full persisted chat history from the database.
        Returns a list of content dicts formatted for Gemini.
        """
        session_data = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        if not session_data:
            return []
        
        # 'full_history' stores the raw turn-by-turn data
        raw_history = session_data.get("full_history", [])
        
        # Convert DB format back to Gemini format if necessary
        # Assuming DB stores: [{'role': 'user', 'parts': ['text']}, ...]
        return raw_history

    async def add_turn(self, thread_id, user_input, model_response):
        """
        Appends a new interaction (User + Model) to the database.
        """
        new_turns = [
            {"role": "user", "parts": [str(user_input)]},
            {"role": "model", "parts": [str(model_response)]}
        ]
        
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$push": {"full_history": {"$each": new_turns}}}
        )

    async def manage_context_window(self, thread_id, current_history):
        """
        Checks token count. If > 1M, prunes oldest messages (preserving index 0 System Prompt).
        Returns: (valid_history, token_count)
        """
        try:
            # We construct a dummy Content object list to count tokens
            contents = []
            for turn in current_history:
                contents.append(
                    genai.protos.Content(
                        role=turn['role'], 
                        parts=[genai.protos.Part(text=p) for p in turn['parts']]
                    )
                )
            
            # Use the API to count tokens accurately
            token_info = await self.model.count_tokens_async(contents)
            total_tokens = token_info.total_tokens

            # Pruning Logic
            if total_tokens > self.max_tokens:
                print(f"⚠️ [RPG Memory] Thread {thread_id} exceeded limit ({total_tokens}). Pruning...")
                # Keep System Prompt (Index 0) + trimmed list
                # Remove chunks of 2 (User+Model) from the beginning of the conversation
                while total_tokens > self.max_tokens and len(current_history) > 2:
                    current_history.pop(1) # Remove oldest User msg
                    current_history.pop(1) # Remove oldest Model msg
                    
                    # Recount (Simplified approximation for speed in loop)
                    contents = [
                         genai.protos.Content(
                            role=t['role'], 
                            parts=[genai.protos.Part(text=str(p)) for p in t['parts']]
                        ) for t in current_history
                    ]
                    total_tokens = (await self.model.count_tokens_async(contents)).total_tokens
                
                # Update DB with pruned history
                rpg_sessions_collection.update_one(
                    {"thread_id": int(thread_id)},
                    {"$set": {"full_history": current_history}}
                )

            # Save current token usage to DB for reference
            rpg_sessions_collection.update_one(
                {"thread_id": int(thread_id)},
                {"$set": {"token_usage": total_tokens}}
            )
            
            return current_history, total_tokens

        except Exception as e:
            print(f"❌ [RPG Memory Error] Token Count Failed: {e}")
            return current_history, 0

    async def initialize_session(self, thread_id, system_prompt):
        """Sets up the initial history with the System Prompt."""
        initial_history = [{"role": "user", "parts": [system_prompt]}] # Gemini often treats System instructions as first User msg or System role
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$set": {
                "full_history": initial_history, 
                "token_usage": 0
            }}
        )
        return initial_history