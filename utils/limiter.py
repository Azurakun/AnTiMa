# utils/limiter.py
import time
from utils.db import ai_config_collection

class RateLimiter:
    def __init__(self):
        # Storage: { "func_name": { "user": {id: []}, "guild": {id: []} } }
        self._usage = {
            "antima": {"user": {}, "guild": {}},
            "rpg": {"user": {}, "guild": {}}
        }
        
        # Defaults (Fetched from DB on usage)
        # Structure: { "antima": { "user_limit": 5, "user_window": 60, "guild_limit": 20, "guild_window": 60 } }
        self._config_cache = {}

    def _get_config(self, func_name):
        """Fetches config, using cache or DB."""
        if func_name in self._config_cache:
            return self._config_cache[func_name]
        
        doc = ai_config_collection.find_one({"_id": "GLOBAL_RATE_LIMITS"}) or {}
        defaults = {
            "user_limit": 5, "user_window": 60,
            "guild_limit": 20, "guild_window": 60
        }
        config = doc.get(func_name, defaults)
        self._config_cache[func_name] = config
        return config

    def _clean_history(self, history, window):
        now = time.time()
        return [t for t in history if now - t < window]

    def check_available(self, user_id: int, guild_id: int, func_name: str) -> bool:
        """
        Checks if the user OR the server has quota available. 
        Does NOT consume tokens yet.
        """
        config = self._get_config(func_name)
        
        # 1. Check User Limit
        user_hist = self._usage[func_name]["user"].get(user_id, [])
        user_hist = self._clean_history(user_hist, config['user_window'])
        if len(user_hist) < config['user_limit']:
            return True # User has quota

        # 2. Check Server Limit (Fallback)
        if guild_id:
            guild_hist = self._usage[func_name]["guild"].get(guild_id, [])
            guild_hist = self._clean_history(guild_hist, config['guild_window'])
            if len(guild_hist) < config['guild_limit']:
                return True # Server has quota

        return False # Both exhausted

    def consume(self, user_id: int, guild_id: int, func_name: str) -> str:
        """
        Reduces the limit counter. 
        Prioritizes User quota. Falls back to Server quota.
        Returns 'user', 'server', or 'none' (if blocked).
        """
        config = self._get_config(func_name)
        now = time.time()

        # 1. Try Consume User
        if func_name not in self._usage: self._usage[func_name] = {"user": {}, "guild": {}}
        
        user_hist = self._usage[func_name]["user"].get(user_id, [])
        user_hist = self._clean_history(user_hist, config['user_window'])
        
        if len(user_hist) < config['user_limit']:
            user_hist.append(now)
            self._usage[func_name]["user"][user_id] = user_hist
            return "user"

        # 2. Try Consume Server
        if guild_id:
            guild_hist = self._usage[func_name]["guild"].get(guild_id, [])
            guild_hist = self._clean_history(guild_hist, config['guild_window'])
            
            if len(guild_hist) < config['guild_limit']:
                guild_hist.append(now)
                self._usage[func_name]["guild"][guild_id] = guild_hist
                return "server"

        return "none"

    def update_limits(self, func_name: str, scope: str, limit: int, window: int):
        """Updates limits in DB and clears cache."""
        key_limit = f"{scope}_limit"
        key_window = f"{scope}_window"
        
        ai_config_collection.update_one(
            {"_id": "GLOBAL_RATE_LIMITS"},
            {"$set": {
                f"{func_name}.{key_limit}": limit,
                f"{func_name}.{key_window}": window
            }},
            upsert=True
        )
        if func_name in self._config_cache:
            del self._config_cache[func_name] # Invalidate cache

limiter = RateLimiter()