# utils/limiter.py
import time
from utils.db import ai_config_collection

class RateLimiter:
    def __init__(self):
        # Memory Storage for timestamps: 
        # { "func_name": { "user": {id: [timestamps]}, "guild": {id: [timestamps]} } }
        self._usage = {}
        
        # Cache for Global Defaults
        self._global_config_cache = {}

    def _get_global_config(self, func_name):
        """Fetches global defaults (limit & window) from DB."""
        if func_name in self._global_config_cache:
            return self._global_config_cache[func_name]
        
        doc = ai_config_collection.find_one({"_id": "GLOBAL_RATE_LIMITS"}) or {}
        
        # Defaults if global config is missing
        defaults = {
            "user_limit": 5, "user_window": 60,   
            "guild_limit": 20, "guild_window": 60 
        }
        
        config = doc.get(func_name, defaults)
        self._global_config_cache[func_name] = config
        return config

    def _get_specific_limit(self, target_id: str, scope: str, func_name: str, global_default: int):
        """
        Checks if a specific User or Guild has a custom limit override in DB.
        If found, returns it. Otherwise returns global_default.
        """
        # ID Convention: Users are "USER_123", Guilds are just "123"
        db_id = f"USER_{target_id}" if scope == "user" else str(target_id)
        
        doc = ai_config_collection.find_one({"_id": db_id}, {"limits": 1})
        
        if doc and "limits" in doc and func_name in doc["limits"]:
            return int(doc["limits"][func_name])
        
        return global_default

    def _ensure_keys(self, func_name):
        if func_name not in self._usage:
            self._usage[func_name] = {"user": {}, "guild": {}}

    def _clean_history(self, history, window):
        now = time.time()
        return [t for t in history if now - t < window]

    def check_available(self, user_id: int, guild_id: int, func_name: str) -> bool:
        """
        Checks availability using Specific Limits -> Global Limits.
        Does NOT consume quota.
        """
        self._ensure_keys(func_name)
        global_conf = self._get_global_config(func_name)
        
        # 1. CHECK USER (Specific -> Global)
        limit_user = self._get_specific_limit(user_id, "user", func_name, global_conf['user_limit'])
        window_user = global_conf['user_window'] # Window is always global for simplicity
        
        user_hist = self._usage[func_name]["user"].get(user_id, [])
        user_hist = self._clean_history(user_hist, window_user)
        self._usage[func_name]["user"][user_id] = user_hist # Save cleaned
        
        if len(user_hist) < limit_user:
            return True # User has quota

        # 2. CHECK GUILD (Specific -> Global) - Fallback
        if guild_id:
            limit_guild = self._get_specific_limit(guild_id, "guild", func_name, global_conf['guild_limit'])
            window_guild = global_conf['guild_window']
            
            guild_hist = self._usage[func_name]["guild"].get(guild_id, [])
            guild_hist = self._clean_history(guild_hist, window_guild)
            self._usage[func_name]["guild"][guild_id] = guild_hist
            
            if len(guild_hist) < limit_guild:
                return True # Guild has quota

        return False # Both exhausted

    def consume(self, user_id: int, guild_id: int, func_name: str) -> str:
        """
        Reduces quota.
        Priority: User Quota -> Server Quota.
        Returns: 'user', 'server', or 'none'.
        """
        self._ensure_keys(func_name)
        global_conf = self._get_global_config(func_name)
        now = time.time()

        # 1. Try Consume User
        limit_user = self._get_specific_limit(user_id, "user", func_name, global_conf['user_limit'])
        user_hist = self._usage[func_name]["user"].get(user_id, [])
        user_hist = self._clean_history(user_hist, global_conf['user_window'])
        
        if len(user_hist) < limit_user:
            user_hist.append(now)
            self._usage[func_name]["user"][user_id] = user_hist
            return "user"

        # 2. Try Consume Server
        if guild_id:
            limit_guild = self._get_specific_limit(guild_id, "guild", func_name, global_conf['guild_limit'])
            guild_hist = self._usage[func_name]["guild"].get(guild_id, [])
            guild_hist = self._clean_history(guild_hist, global_conf['guild_window'])
            
            if len(guild_hist) < limit_guild:
                guild_hist.append(now)
                self._usage[func_name]["guild"][guild_id] = guild_hist
                return "server"

        return "none"

    def set_override(self, target_id: str, scope: str, func_name: str, limit: int):
        """Sets a specific limit for a User or Guild in the DB."""
        db_id = f"USER_{target_id}" if scope == "user" else str(target_id)
        
        ai_config_collection.update_one(
            {"_id": db_id},
            {"$set": {f"limits.{func_name}": limit}},
            upsert=True
        )

# Global Instance
limiter = RateLimiter()