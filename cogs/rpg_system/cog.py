# cogs/rpg_system/cog.py
import discord
from discord import app_commands
from discord.ext import commands, tasks
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import uuid
from datetime import datetime

from utils.db import (
    ai_config_collection, rpg_sessions_collection, rpg_inventory_collection,
    rpg_world_state_collection, web_actions_collection, rpg_web_tokens_collection, db
)
from utils.limiter import limiter
from .config import RPG_CLASSES
from .ui import AdventureSetupView, CloseVoteView
from .memory import RPGContextManager
from .engine import RPGEngine
from .utils import RPGLogger
from . import prompts, tools

WEB_DASHBOARD_URL = "http://0.0.0.0:8000/"

class RPGAdventureCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.model = None
        self.engine = None
        self.memory_manager = None
        
        try:
            safety_settings = {
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            }
            
            self.model = genai.GenerativeModel(
                'gemini-2.5-pro',
                tools=[
                    tools.grant_item_to_player, tools.apply_damage, 
                    tools.apply_healing, tools.deduct_mana, 
                    tools.roll_d20, tools.update_journal,
                    tools.update_world_entity, tools.update_environment,
                    tools.manage_story_log
                ],
                safety_settings=safety_settings
            )
            
            self.memory_manager = RPGContextManager(self.model)
            self.engine = RPGEngine(bot, self.model, self.memory_manager)
            print("✅ RPG System Online.")
            
        except Exception as e:
            print(f"❌ Failed to load Gemini RPG: {e}")

        self.cleanup_tasks.start()
        self.web_poller.start()

    def cog_unload(self):
        self.cleanup_tasks.cancel()
        self.web_poller.cancel()

    # --- TASKS ---

    @tasks.loop(seconds=10)
    async def cleanup_tasks(self):
        try:
            for session in rpg_sessions_collection.find({"delete_requested": True}):
                tid = session['thread_id']
                try: 
                    ch = self.bot.get_channel(tid) or await self.bot.fetch_channel(tid)
                    if ch: await ch.delete()
                except: pass
                
                rpg_sessions_collection.delete_one({"thread_id": tid})
                rpg_world_state_collection.delete_one({"thread_id": tid})
                db.rpg_debug_terminal.delete_many({"thread_id": str(tid)})
                
                if tid in self.engine.active_sessions: 
                    del self.engine.active_sessions[tid]
        except Exception as e: print(f"Cleanup Error: {e}")

    @tasks.loop(seconds=3)
    async def web_poller(self):
        try:
            actions = list(web_actions_collection.find({"type": "create_rpg_web", "status": "pending"}))
            for action in actions:
                try:
                    web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "processing"}})
                    guild = self.bot.get_guild(action["guild_id"])
                    user = guild.get_member(action["user_id"]) if guild else None
                    
                    if user and guild:
                        data = action["data"]
                        c = data["character"]
                        profile = {
                            "name": c.get("name"), "class": c.get("class"), "hp": 100, "max_hp": 100, "mp": 50, "max_mp": 50,
                            "stats": c.get("stats"), "skills": ["Custom Action"], "alignment": c.get("alignment"),
                            "backstory": c.get("backstory"), "age": c.get("age"), "pronouns": c.get("pronouns", "They/Them"),
                            "appearance": c.get("appearance", "Unknown"), "personality": c.get("personality", "Unknown"),
                            "hobbies": c.get("hobbies", "None")
                        }
                        await self.engine.create_adventure_thread(
                            interaction=None, lore=data["lore"], players=[user], profiles={user.id: profile},
                            scenario_name=data["scenario"], story_mode=data["story_mode"],
                            custom_title=data["title"], manual_guild_id=guild.id, manual_user=user
                        )
                        web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "completed"}})
                    else:
                        web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "failed", "reason": "User/Guild not found"}})
                except Exception as e:
                    web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "error", "error": str(e)}})
        except Exception as e: print(f"Poller Error: {e}")

    # --- CALLBACKS ---

    async def reroll_turn_callback(self, interaction, thread_id):
        session = rpg_sessions_collection.find_one({"thread_id": thread_id})
        if not session or interaction.user.id != session['owner_id']:
             return await interaction.followup.send("⚠️ Only the Game Master can reroll.", ephemeral=True)
        
        try: await interaction.message.delete()
        except: pass 

        deleted_turn = self.memory_manager.delete_last_turn(thread_id)
        RPGLogger.log(thread_id, "info", "Turn Rerolled by User (State Rewound)")

        if deleted_turn:
            m_ids = deleted_turn.get("bot_message_id")
            if m_ids:
                if isinstance(m_ids, list):
                    for m in m_ids: 
                        try: await (await interaction.channel.fetch_message(m)).delete()
                        except: pass
                else: 
                    try: await (await interaction.channel.fetch_message(int(m_ids))).delete()
                    except: pass
        
        prompt = "Continue"
        msg_id = None
        history = session.get("turn_history", [])
        
        if history:
            last = history[-1]
            prompt = last.get("input", "Continue")
            msg_id = last.get("user_message_id")

        if thread_id in self.engine.active_sessions: 
            del self.engine.active_sessions[thread_id]
            
        await self.engine.process_turn(interaction.channel, prompt, is_reroll=True, message_id=msg_id)

    # --- COMMANDS ---

    rpg_group = app_commands.Group(name="rpg", description="⚔️ Play immersive role-playing adventures.")

    @rpg_group.command(name="start", description="Start a new adventure.")
    async def rpg_start(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        config = ai_config_collection.find_one({"_id": str(interaction.guild_id)})
        if not config or "rpg_channel_id" not in config: 
            return await interaction.followup.send("⚠️ Admin must set channel first using `/config rpg`.", ephemeral=True)
        view = AdventureSetupView(self.bot, interaction.user)
        view.message = await interaction.followup.send(content=f"⚔️ **RPG Lobby** {interaction.user.mention}", embed=view._get_party_embed(), view=view)

    @rpg_group.command(name="world", description="Inspect World State & Quests.")
    async def rpg_world(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread): 
            return await interaction.response.send_message("Use this inside an active Adventure Thread.", ephemeral=True)
        
        world_data = rpg_world_state_collection.find_one({"thread_id": interaction.channel.id})
        if not world_data: return await interaction.response.send_message("No world data found.", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)

        embeds = []
        env = world_data.get("environment", {})
        quests = world_data.get("quests", {})
        
        emb_env = discord.Embed(title=f"🕰️ World Clock", color=discord.Color.light_grey())
        emb_env.add_field(name="Time", value=env.get("time", "Unknown"), inline=True)
        emb_env.add_field(name="Weather", value=env.get("weather", "Clear"), inline=True)
        embeds.append(emb_env)

        active_qs = [q for q in quests.values() if q.get("status") == "active"]
        if active_qs:
            emb = discord.Embed(title="🛡️ Active Objectives", color=discord.Color.gold())
            for q in active_qs: emb.add_field(name=q['name'], value=q['details'], inline=False)
            embeds.append(emb)

        view = discord.ui.View()
        url = f"{WEB_DASHBOARD_URL}/rpg/inspect/{interaction.channel.id}"
        view.add_item(discord.ui.Button(label="🧠 Open Memory Inspector", url=url, style=discord.ButtonStyle.link, emoji="🔗"))
        
        await interaction.followup.send(content="🌍 **World State Summary**", embeds=embeds, view=view)

    @rpg_group.command(name="rewind", description="Rewind story to a specific turn ID.")
    async def rpg_rewind(self, interaction: discord.Interaction, turn_id: int):
        if not isinstance(interaction.channel, discord.Thread): return
        
        await interaction.response.send_message(f"⏳ **Rewinding to Turn {turn_id}...**", ephemeral=True)
        deleted_turns, rewind_ts = self.memory_manager.trim_history(interaction.channel.id, turn_id)
        
        if rewind_ts:
            # FIXED: Uses purge_memories with specific turn ID for deterministic cleanup
            await self.memory_manager.purge_memories(interaction.channel.id, rewind_ts, from_turn_id=turn_id)
            
        if deleted_turns:
            for turn in deleted_turns:
                try: 
                    if turn.get("user_message_id"): await (await interaction.channel.fetch_message(int(turn["user_message_id"]))).delete()
                except: pass
                b_ids = turn.get("bot_message_id")
                if b_ids:
                    ids = b_ids if isinstance(b_ids, list) else [b_ids]
                    for mid in ids:
                        try: await (await interaction.channel.fetch_message(int(mid))).delete()
                        except: pass
        
        if interaction.channel.id in self.engine.active_sessions: 
            del self.engine.active_sessions[interaction.channel.id]
            
        await interaction.followup.send(f"✅ Rewind Complete.", ephemeral=True)

    @rpg_group.command(name="sync", description="Re-read history and sync memory.")
    async def rpg_sync(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread): 
            return await interaction.response.send_message("Threads only.", ephemeral=True)
        
        await interaction.response.send_message("🔄 **Syncing...** [1/4] 📥 Initializing Engine...", ephemeral=False)
        
        try:
            inter_msg = await interaction.original_response()
            status_msg = await interaction.channel.fetch_message(inter_msg.id)
            
            # CALL ENGINE
            total, chunks = await self.engine.sync_session(interaction.channel, status_msg)
            
            view = discord.ui.View()
            url = f"{WEB_DASHBOARD_URL}/rpg/inspect/{interaction.channel.id}"
            view.add_item(discord.ui.Button(label="🧠 Check Inspector", url=url, style=discord.ButtonStyle.link))
            
            await status_msg.edit(content=f"✅ **Sync Complete:**\n- 📜 Rebuilt **{total}** Turns.\n- 🗂️ Indexed **{chunks}** Memories.\n- 🧹 **World State Preserved.**", view=view)
        
        except Exception as e:
            await interaction.followup.send(f"❌ Sync Failed: {e}", ephemeral=True)

    @rpg_group.command(name="history", description="View turn history.")
    async def rpg_history(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread): return
        session = rpg_sessions_collection.find_one({"thread_id": interaction.channel.id})
        history = session.get("turn_history", []) if session else []
        desc = ""
        for i in range(max(0, len(history) - 10), len(history)):
            t = history[i]
            desc += f"**Turn {t.get('turn_id', i+1)}**: {t['input'][:50]}...\n"
        await interaction.response.send_message(embed=discord.Embed(title="📜 History", description=desc or "Empty.", color=discord.Color.blue()), ephemeral=True)

    @rpg_group.command(name="mode", description="Switch Game Mode.")
    @app_commands.choices(mode=[app_commands.Choice(name="Standard", value="standard"), app_commands.Choice(name="Story", value="story")])
    async def rpg_mode(self, interaction: discord.Interaction, mode: str):
        rpg_sessions_collection.update_one({"thread_id": interaction.channel.id}, {"$set": {"story_mode": (mode=="story")}})
        await interaction.response.send_message(f"Mode set to: **{mode.upper()}**")

    @rpg_group.command(name="web_new", description="Create an adventure via the Web Dashboard.")
    async def rpg_web_new(self, interaction: discord.Interaction):
        token = str(uuid.uuid4())
        rpg_web_tokens_collection.insert_one({"token": token, "user_id": interaction.user.id, "guild_id": interaction.guild_id, "status": "pending", "created_at": datetime.utcnow()})
        url = f"{WEB_DASHBOARD_URL}/rpg/setup?token={token}"
        embed = discord.Embed(title="🌐 Web Setup", description=f"[**Click Here**]({url})", color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rpg_group.command(name="personas", description="Manage your saved characters.")
    async def rpg_personas(self, interaction: discord.Interaction):
        token = str(uuid.uuid4())
        rpg_web_tokens_collection.insert_one({"token": token, "user_id": interaction.user.id, "guild_id": interaction.guild_id, "status": "pending", "type": "persona_management", "created_at": datetime.utcnow()})
        url = f"{WEB_DASHBOARD_URL}/rpg/personas?token={token}"
        embed = discord.Embed(title="🎭 Persona Manager", description=f"[**Click Here**]({url})", color=discord.Color.purple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rpg_group.command(name="end", description="End the adventure session.")
    async def rpg_end(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread): return
        session = rpg_sessions_collection.find_one({"thread_id": interaction.channel.id})
        if not session: return
        
        if interaction.user.id == session.get("owner_id"): 
            rpg_sessions_collection.update_one({"thread_id": interaction.channel.id}, {"$set": {"active": False}})
            await interaction.channel.send("📕 **Adventure Archived.**")
            await interaction.channel.edit(locked=True, archived=True)
            await interaction.response.send_message("Session closed.", ephemeral=True)
        else:
            view = CloseVoteView(self, interaction.channel.id, interaction.user.id, session.get("players", []), session['owner_id'])
            await interaction.response.send_message(f"Vote to end?", view=view)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not isinstance(message.channel, discord.Thread): return
        
        session = rpg_sessions_collection.find_one({"thread_id": message.channel.id})
        if session and message.author.id in session.get("players", []):
            if not session.get("active", True): return
            
            prompt = f"{message.author.name}: {message.content}"
            await self.engine.process_turn(
                channel=message.channel, 
                prompt=prompt, 
                user=message.author, 
                message_id=message.id
            )