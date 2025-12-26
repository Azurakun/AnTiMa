# cogs/rpg_system/cog.py
import discord
from discord import app_commands
from discord.ext import commands, tasks
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import asyncio
import random
import uuid
from datetime import datetime
from utils.db import (
    ai_config_collection, 
    rpg_sessions_collection, 
    rpg_inventory_collection,
    web_actions_collection, 
    rpg_web_tokens_collection,
    db 
)
from utils.limiter import limiter
from .config import RPG_CLASSES
from . import tools
from .ui import RPGGameView, AdventureSetupView, CloseVoteView
from .memory import RPGContextManager

class RPGAdventureCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
        try:
            # Initialize Gemini with ALL tools, including the new World Entity manager
            self.model = genai.GenerativeModel(
                'gemini-2.5-pro',
                tools=[
                    tools.grant_item_to_player, 
                    tools.apply_damage, 
                    tools.apply_healing, 
                    tools.deduct_mana, 
                    tools.roll_d20, 
                    tools.update_journal,
                    tools.update_world_entity  # <--- NEW TOOL for Character Sheets/Locations
                ],
                safety_settings=self.safety_settings
            )
            self.memory_manager = RPGContextManager(self.model)
            self.active_sessions = {} 
        except Exception as e:
            print(f"Failed to load Gemini for RPG: {e}")
        
        self.cleanup_deleted_sessions.start()
        self.poll_web_creations.start()

    def cog_unload(self):
        self.cleanup_deleted_sessions.cancel()
        self.poll_web_creations.cancel()

    @tasks.loop(seconds=10)
    async def cleanup_deleted_sessions(self):
        try:
            to_delete = rpg_sessions_collection.find({"delete_requested": True})
            for session in to_delete:
                thread_id = session['thread_id']
                try:
                    thread = self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
                    if thread: await thread.delete()
                except: pass
                rpg_sessions_collection.delete_one({"thread_id": thread_id})
                if thread_id in self.active_sessions: del self.active_sessions[thread_id]
        except Exception as e: print(f"Cleanup Error: {e}")

    @tasks.loop(seconds=3)
    async def poll_web_creations(self):
        """Checks DB for RPGs created via the Website."""
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
                            "name": c.get("name"),
                            "class": c.get("class"),
                            "hp": 100, "max_hp": 100, "mp": 50, "max_mp": 50,
                            "stats": c.get("stats"),
                            "skills": ["Custom Action"],
                            "alignment": c.get("alignment"),
                            "backstory": c.get("backstory"),
                            "age": c.get("age"),
                            "pronouns": c.get("pronouns", "They/Them"),
                            "appearance": c.get("appearance", "Unknown"),
                            "personality": c.get("personality", "Unknown"),
                            "hobbies": c.get("hobbies", "None")
                        }
                        
                        await self.create_adventure_thread(
                            interaction=None,
                            lore=data["lore"],
                            players=[user],
                            profiles={user.id: profile},
                            scenario_name=data["scenario"],
                            story_mode=data["story_mode"],
                            custom_title=data["title"],
                            manual_guild_id=guild.id,
                            manual_user=user
                        )
                        web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "completed"}})
                    else:
                        web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "failed", "reason": "User/Guild not found"}})
                except Exception as e:
                    print(f"Error processing web RPG: {e}")
                    web_actions_collection.update_one({"_id": action["_id"]}, {"$set": {"status": "error", "error": str(e)}})
        except Exception as e:
            print(f"Poller Error: {e}")

    async def _initialize_session(self, channel_id, session_db, initial_prompt="Resume"):
        """
        Initializes the chat session. 
        Crucially, this uses build_context_block to fetch the 'World Sheet' and 'RAG Memories'.
        """
        chat_session = self.model.start_chat(history=[])
        
        # Build the dynamic context block (fetches from Vector DB and World State)
        memory_block = await self.memory_manager.build_context_block(session_db, initial_prompt)
        
        system_prime = (
            f"SYSTEM: BOOTING DUNGEON MASTER CORE.\n"
            f"{memory_block}\n"
            "**CRITICAL INSTRUCTION: STATE MANAGEMENT & MEMORY**\n"
            "1. **World Sheet (Character Sheets):** The 'World Sheet' in the memory block contains the persistent state of NPCs and Locations.\n"
            "2. **Update Responsibility:** If a NEW named NPC or Location becomes important, OR if an existing one changes significantly (e.g., an NPC dies, a town burns down), YOU MUST use the `update_world_entity` tool to save it.\n"
            "3. **Consistency:** Check the 'Archived Memories' and 'World Sheet' before generating details. Do not contradict established facts.\n"
            "4. **Tone:** Maintain the exact narrative style of the previous logs.\n"
            "5. Wait for the User's input to continue."
        )
        
        try: await chat_session.send_message_async(system_prime)
        except Exception as e: print(f"Failed to prime memory: {e}")

        self.active_sessions[channel_id] = {
            'session': chat_session, 
            'owner_id': session_db['owner_id'], 
            'last_prompt': initial_prompt
        }
        return True

    def get_status_embed(self, thread_id):
        session = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        if not session: return None
        embed = discord.Embed(title="📊 Party Status", color=discord.Color.dark_grey())
        for uid, stats in session["player_stats"].items():
            user = self.bot.get_user(int(uid))
            name = user.display_name if user else f"Player {uid}"
            
            hp_per = max(0.0, min(1.0, stats['hp'] / stats['max_hp']))
            hp_bar = "🟩" * int(hp_per * 8) + "⬛" * (8 - int(hp_per * 8))
            mp_per = max(0.0, min(1.0, stats['mp'] / stats['max_mp']))
            mp_bar = "🟦" * int(mp_per * 8) + "⬛" * (8 - int(mp_per * 8))
            
            stat_lines = []
            base_stats = stats.get('stats', {})
            for key in ["STR", "DEX", "INT", "CHA"]:
                val = base_stats.get(key, 10)
                mod = (val - 10) // 2
                sign = "+" if mod >= 0 else ""
                stat_lines.append(f"**{key}**: {val} (`{sign}{mod}`)")
            
            embed.add_field(
                name=f"{name} ({stats['class']})", 
                value=(
                    f"**HP:** `{stats['hp']}/{stats['max_hp']}` {hp_bar}\n"
                    f"**MP:** `{stats['mp']}/{stats['max_mp']}` {mp_bar}\n"
                    f"📊 {' | '.join(stat_lines)}\n"
                    f"✨ {', '.join(stats.get('skills', []))}"
                ), inline=False
            )
        return embed

    async def create_adventure_thread(self, interaction, lore, players, profiles, scenario_name, story_mode=False, custom_title=None, manual_guild_id=None, manual_user=None):
        if interaction:
            guild_id = interaction.guild_id
            owner = interaction.user
            respond = interaction.followup.send
        else:
            guild_id = manual_guild_id
            owner = manual_user
            respond = None
            
        config = ai_config_collection.find_one({"_id": str(guild_id)})
        channel = self.bot.get_channel(config.get("rpg_channel_id"))
        
        if not channel:
            if respond: await respond("RPG Channel not set!")
            return

        if custom_title: title = custom_title
        else:
            try:
                prompt = f"Generate a unique 5-word title for an RPG adventure. Scenario: {scenario_name}. Lore: {lore[:100]}."
                resp = await self.model.generate_content_async(prompt)
                title = resp.text.strip().replace('"', '')[:50]
            except: title = f"Quest: {owner.name}"

        thread = await channel.create_thread(name=title, type=discord.ChannelType.private_thread, auto_archive_duration=10080)
        for p in players: await thread.add_user(p)

        player_stats_db = {str(p.id): profiles.get(p.id, RPG_CLASSES["Freelancer"]) for p in players}

        session_data = {
            "thread_id": thread.id, "guild_id": guild_id, "owner_id": owner.id, "owner_name": owner.name,
            "title": title, "players": [p.id for p in players], "player_stats": player_stats_db, 
            "scenario_type": scenario_name, "campaign_log": [], "turn_history": [], "npc_registry": [], "quest_log": [], 
            "created_at": datetime.utcnow(), "last_active": datetime.utcnow(), "active": True, 
            "delete_requested": False, "story_mode": story_mode
        }
        rpg_sessions_collection.insert_one(session_data)
        
        if respond: await respond(f"✅ Adventure **{title}** created! Check {thread.mention}")
        else: await channel.send(f"⚔️ **New Web-Created Adventure:** {owner.mention} begins **{title}**! -> {thread.mention}")

        char_details = []
        for pid, pdata in player_stats_db.items():
            detail = f"Player {pid} ({pdata.get('name', 'Unknown')}):\n"
            detail += f"   - Class: {pdata.get('class')} | Age: {pdata.get('age')}\n"
            detail += f"   - Pronouns: {pdata.get('pronouns', 'N/A')} | Alignment: {pdata.get('alignment', 'N/A')}\n"
            detail += f"   - Appearance: {pdata.get('appearance', 'N/A')}\n"
            detail += f"   - Personality: {pdata.get('personality', 'N/A')}\n"
            detail += f"   - Hobbies: {pdata.get('hobbies', 'N/A')}\n"
            if 'backstory' in pdata: detail += f"   - Backstory: {pdata['backstory']}\n"
            char_details.append(detail)

        mechanics = (
            "2. **Story Mode Active:** NO DICE. Narrative focus only. Do not track HP/MP." 
            if story_mode else 
            "2. **Standard Mode:** Use `roll_d20` for risks. Track HP/MP/Ammo via tools."
        )

        sys_prompt = (
            f"You are the **Dungeon Master** for a fictional tabletop RPG. "
            f"**SCENARIO:** {scenario_name}\n"
            f"**GUIDELINES:**\n"
            f"1. **Fiction Only:** Combat/Conflict allowed.\n"
            f"{mechanics}\n"
            f"3. **Memory:** Use `update_journal` for big events, `update_world_entity` for NPCs.\n"
            f"4. **Immersion:** Be descriptive.\n"
            f"**LORE:** {lore}\n"
            f"**PARTY DETAILS:**\n{'\n'.join(char_details)}\n"
            f"**START:** The players have gathered. Set the opening scene vividly, incorporating their personas."
        )
        
        await self._initialize_session(thread.id, session_data, "Start Adventure")
        await self.process_game_turn(thread, sys_prompt)

    async def reroll_turn_callback(self, interaction, thread_id):
        session_db = rpg_sessions_collection.find_one({"thread_id": thread_id})
        if not session_db or interaction.user.id != session_db['owner_id']:
             return await interaction.followup.send("Leader only.", ephemeral=True)
        
        try: await interaction.message.delete()
        except: pass 
        
        last_turn = None
        if session_db.get("turn_history"):
            last_turn = session_db["turn_history"][-1]

        self.memory_manager.delete_last_turn(thread_id)
        
        prompt = "Continue" 
        msg_id = None
        
        if last_turn:
            prompt = last_turn.get("input", "Continue")
            msg_id = last_turn.get("user_message_id")
            if msg_id:
                try:
                    original_msg = await interaction.channel.fetch_message(int(msg_id))
                    if original_msg and original_msg.content:
                        prompt = f"{original_msg.author.name}: {original_msg.content}"
                except Exception: pass

        if thread_id in self.active_sessions:
            del self.active_sessions[thread_id]

        await self.process_game_turn(interaction.channel, prompt, is_reroll=True, message_id=msg_id)

    async def process_game_turn(self, channel, prompt, user=None, is_reroll=False, message_id=None):
        if user and not limiter.check_available(user.id, channel.guild.id, "rpg_gen"):
            return await channel.send("⏳ Quota Exceeded.")

        session_db = rpg_sessions_collection.find_one({"thread_id": channel.id})
        if not session_db: return
        
        # 1. Archive Old Turns (Free up context window space)
        await self.memory_manager.archive_old_turns(channel.id, session_db)
        
        # 2. Re-Initialize Session (or refresh context) to include RAG & World Sheet for THIS turn
        # We perform a "Soft Refresh" by creating a new session or injecting context. 
        # For simplicity and robustness, we re-initialize the session with the latest state.
        await self._initialize_session(channel.id, session_db, prompt)
        
        data = self.active_sessions[channel.id]
        chat_session = data['session']
        rpg_sessions_collection.update_one({"thread_id": channel.id}, {"$set": {"last_active": datetime.utcnow()}})
        
        if not is_reroll:
            data['last_prompt'] = prompt
            data['last_roll_result'] = None
        
        async with channel.typing():
            story_mode = session_db.get("story_mode", False)
            mechanics_instr = (
                "**MODE: STORY (No Dice/Stats).** Focus on narrative." 
                if story_mode else 
                "**MODE: STANDARD.** Use `roll_d20` for checks. Use tools for HP/MP."
            )
            
            # 3. Construct the Dynamic Prompt
            # We explicitly mention the "System Memory" block that was injected during _initialize_session
            full_prompt = (
                f"**USER ACTION:** {prompt}\n"
                f"**DM INSTRUCTIONS:**\n"
                f"{mechanics_instr}\n"
                f"1. **CONSULT MEMORY:** Check the 'System Memory' block provided at start of chat for NPC details and past events.\n"
                f"2. **WORLD UPDATES:** If you introduce a NEW named NPC or important Location, use `update_world_entity` immediately.\n"
                f"3. **IMMERSION:** Describe sights/sounds vividly.\n"
                f"4. **OUTPUT:** Narrate the outcome. Do NOT leave blank.\n"
                f"{'Reroll requested. Change the outcome.' if is_reroll else ''}"
            )
            
            try:
                response = await chat_session.send_message_async(full_prompt)
                turns = 0
                text_content = ""
                
                # Tool Loop
                while response.parts and response.parts[0].function_call and turns < 10:
                    turns += 1
                    fn = response.parts[0].function_call
                    res_txt = "Error"
                    
                    if fn.name == "roll_d20":
                        if story_mode: res_txt = "Dice disabled in Story Mode."
                        elif is_reroll and data.get('last_roll_result'): res_txt = f"LOCKED: {data['last_roll_result']}"
                        else:
                            diff, mod = int(fn.args.get("difficulty", 10)), int(fn.args.get("modifier", 0))
                            roll = random.randint(1, 20)
                            total = roll + mod
                            success = total >= diff
                            desc = f"🎲 **{roll}** (d20) {f'+ {mod}' if mod >= 0 else f'- {abs(mod)}'} = **{total}** vs DC {diff}"
                            color = discord.Color.green() if success else discord.Color.red()
                            if roll == 20: 
                                color = discord.Color.gold()
                                desc += " **(CRIT!)**"
                            elif roll == 1: desc += " **(FAIL!)**"
                            await channel.send(embed=discord.Embed(title=f"🎲 {fn.args.get('check_type', 'Check')}", description=desc, color=color))
                            res_txt = f"Roll: {roll}, Total: {total}, DC: {diff}, Success: {success}"
                            if not is_reroll: data['last_roll_result'] = res_txt
                    
                    # --- NEW TOOL HANDLER ---
                    elif fn.name == "update_world_entity":
                        res_txt = tools.update_world_entity(
                            str(channel.id), 
                            fn.args["category"], 
                            fn.args["name"], 
                            fn.args["details"], 
                            fn.args.get("status", "active")
                        )
                    # ------------------------

                    elif fn.name == "grant_item_to_player": res_txt = tools.grant_item_to_player(fn.args["user_id"], fn.args["item_name"], fn.args["description"])
                    elif fn.name == "apply_damage": res_txt = "Story Mode." if story_mode else tools.apply_damage(str(channel.id), fn.args["user_id"], fn.args["damage_amount"])
                    elif fn.name == "apply_healing": res_txt = "Story Mode." if story_mode else tools.apply_healing(str(channel.id), fn.args["user_id"], fn.args["heal_amount"])
                    elif fn.name == "deduct_mana": res_txt = "Story Mode." if story_mode else tools.deduct_mana(str(channel.id), fn.args["user_id"], fn.args["mana_cost"])
                    elif fn.name == "update_journal": res_txt = tools.update_journal(str(channel.id), fn.args.get("log_entry"))
                    
                    response = await chat_session.send_message_async(genai.protos.Content(parts=[genai.protos.Part(function_response=genai.protos.FunctionResponse(name=fn.name, response={'result': res_txt}))]))

                try: text_content = response.text
                except ValueError: text_content = "" 

                if not text_content.strip():
                    text_content = "**[System Notice]** Action executed. (Narrative unavailable)"

                current_history = session_db.get("turn_history", [])
                current_turn_id = len(current_history) + 1
                
                footer_text = await self.memory_manager.get_token_count_and_footer(chat_session, turn_id=current_turn_id)
                
                # Smart Chunking
                chunks = [text_content[i:i+4000] for i in range(0, len(text_content), 4000)]
                if not chunks: chunks = ["..."] 
                bot_message_ids = []

                for i, chunk in enumerate(chunks):
                    is_last = (i == len(chunks) - 1)
                    story_emb = discord.Embed(description=chunk, color=discord.Color.from_rgb(47, 49, 54))
                    if i == 0: story_emb.set_author(name="The Dungeon Master", icon_url=self.bot.user.avatar.url if self.bot.user.avatar else None)
                    
                    embeds = [story_emb]
                    view = None
                    
                    if is_last:
                        story_emb.set_footer(text=footer_text)
                        if not story_mode:
                            stat_emb = self.get_status_embed(channel.id)
                            if stat_emb: embeds.append(stat_emb)
                        view = RPGGameView(self, channel.id)

                    bot_msg = await channel.send(embeds=embeds, view=view)
                    bot_message_ids.append(bot_msg.id)

                self.memory_manager.save_turn(
                    channel.id, 
                    user.name if user else "System", 
                    prompt, 
                    text_content, 
                    user_message_id=message_id, 
                    bot_message_id=bot_message_ids
                )
                
                if user: limiter.consume(user.id, channel.guild.id, "rpg_gen")
            except Exception as e:
                await channel.send(f"⚠️ Game Error: {e}")
                print(f"RPG Error: {e}")

    async def close_session(self, thread_id, channel):
        rpg_sessions_collection.update_one({"thread_id": thread_id}, {"$set": {"active": False, "ended_at": datetime.utcnow()}})
        if thread_id in self.active_sessions: del self.active_sessions[thread_id]
        try:
            await channel.send("📕 **Adventure Archived.**")
            await channel.edit(archived=True, locked=True)
        except: pass

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

    @rpg_group.command(name="web_new", description="Create an adventure via the Web Dashboard (Detailed Setup).")
    async def rpg_web_new(self, interaction: discord.Interaction):
        token = str(uuid.uuid4())
        rpg_web_tokens_collection.insert_one({
            "token": token, "user_id": interaction.user.id, "guild_id": interaction.guild_id,
            "status": "pending", "created_at": datetime.utcnow()
        })
        dashboard_url = "https://ray-goniometrical-implausibly.ngrok-free.dev" 
        url = f"{dashboard_url}/rpg/setup?token={token}"
        embed = discord.Embed(title="🌐 Web Setup Initiated", description="Design your adventure with detailed lore, stats, and character backstory.", color=discord.Color.blue())
        embed.add_field(name="Setup Link", value=f"[**Click Here to Create Adventure**]({url})")
        embed.set_footer(text="Link expires once used.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rpg_group.command(name="personas", description="Manage your saved characters via the Web Dashboard.")
    async def rpg_personas(self, interaction: discord.Interaction):
        token = str(uuid.uuid4())
        rpg_web_tokens_collection.insert_one({
            "token": token, 
            "user_id": interaction.user.id, 
            "guild_id": interaction.guild_id,
            "status": "pending", 
            "type": "persona_management",
            "created_at": datetime.utcnow()
        })
        dashboard_url = "https://ray-goniometrical-implausibly.ngrok-free.dev" 
        url = f"{dashboard_url}/rpg/personas?token={token}"
        embed = discord.Embed(title="🎭 Persona Manager", description="Create, edit, or delete your saved characters.", color=discord.Color.purple())
        embed.add_field(name="Management Link", value=f"[**Open Persona Manager**]({url})")
        embed.set_footer(text="Link expires once used.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rpg_group.command(name="history", description="View turn history to find a rewind point.")
    async def rpg_history(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread): 
            return await interaction.response.send_message("Threads only.", ephemeral=True)
        
        session = rpg_sessions_collection.find_one({"thread_id": interaction.channel.id})
        if not session or "turn_history" not in session: 
            return await interaction.response.send_message("No history found.", ephemeral=True)
            
        history = session["turn_history"]
        if not history: return await interaction.response.send_message("History is empty.", ephemeral=True)

        desc = ""
        # Show last 10 turns
        start_index = max(0, len(history) - 10)
        for i in range(start_index, len(history)):
            turn = history[i]
            snippet = (turn['input'][:50] + '...') if len(turn['input']) > 50 else turn['input']
            desc += f"**Turn {i+1}**: {snippet}\n"
        
        embed = discord.Embed(title="📜 Recent History", description=desc, color=discord.Color.blue())
        embed.set_footer(text="Use /rpg rewind <Turn ID> to revert state.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rpg_group.command(name="rewind", description="Rewind the story to a specific turn (Deletes messages).")
    async def rpg_rewind(self, interaction: discord.Interaction, turn_id: int):
        if not isinstance(interaction.channel, discord.Thread): 
            return await interaction.response.send_message("Threads only.", ephemeral=True)
            
        session = rpg_sessions_collection.find_one({"thread_id": interaction.channel.id})
        if not session or interaction.user.id != session.get('owner_id'):
            return await interaction.response.send_message("Leader only.", ephemeral=True)

        history = session.get("turn_history", [])
        target_index = turn_id - 1 
        
        if target_index < 0 or target_index >= len(history):
            return await interaction.response.send_message(f"Invalid Turn ID. Max: {len(history)}", ephemeral=True)
            
        await interaction.response.send_message(f"⏳ **Rewinding to Turn {turn_id}...** (Clearing messages)", ephemeral=True)
        
        deleted_turns = self.memory_manager.trim_history(interaction.channel.id, target_index)
        
        if deleted_turns:
            for turn in deleted_turns:
                if turn.get("user_message_id"):
                    try:
                        msg = await interaction.channel.fetch_message(int(turn["user_message_id"]))
                        await msg.delete()
                    except: pass
                
                b_ids = turn.get("bot_message_id")
                if b_ids:
                    if isinstance(b_ids, list):
                        for bid in b_ids:
                            try: await (await interaction.channel.fetch_message(bid)).delete()
                            except: pass
                    else:
                        try: await (await interaction.channel.fetch_message(int(b_ids))).delete()
                        except: pass
                    
        if interaction.channel.id in self.active_sessions:
            del self.active_sessions[interaction.channel.id]
            
        await interaction.followup.send(f"✅ Rewind Complete! Resuming from **Turn {turn_id}**.", ephemeral=True)

    @rpg_group.command(name="mode", description="Switch Game Mode.")
    @app_commands.choices(mode=[app_commands.Choice(name="Standard", value="standard"), app_commands.Choice(name="Story", value="story")])
    async def rpg_mode(self, interaction: discord.Interaction, mode: str):
        if not isinstance(interaction.channel, discord.Thread): return await interaction.response.send_message("Threads only.", ephemeral=True)
        rpg_sessions_collection.update_one({"thread_id": interaction.channel.id}, {"$set": {"story_mode": (mode=="story")}})
        await interaction.response.send_message(f"Game Mode switched to **{mode.upper()}**.")

    @rpg_group.command(name="end", description="End session.")
    async def rpg_end(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread): return await interaction.response.send_message("Threads only.", ephemeral=True)
        session = rpg_sessions_collection.find_one({"thread_id": interaction.channel.id})
        if not session: return await interaction.response.send_message("No session.", ephemeral=True)
        
        if interaction.user.id == session.get("owner_id"):
             await interaction.response.send_message("🛑 **Stopping adventure...**")
             await self.close_session(interaction.channel.id, interaction.channel)
             return
        
        view = CloseVoteView(self, interaction.channel.id, interaction.user.id, session.get("players", []), session['owner_id'])
        await interaction.response.send_message(f"Vote to end?", view=view)

    @rpg_group.command(name="inventory", description="Check inventory.")
    async def rpg_inventory(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        data = rpg_inventory_collection.find_one({"user_id": target.id})
        embed = discord.Embed(title=f"🎒 {target.display_name}", color=discord.Color.gold())
        if data and data.get("items"):
            for item in data["items"]: embed.add_field(name=item['name'], value=item['desc'], inline=False)
        else: embed.description = "Empty."
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not isinstance(message.channel, discord.Thread): return
        session = rpg_sessions_collection.find_one({"thread_id": message.channel.id})
        if session and message.author.id in session.get("players", []):
            await self.process_game_turn(message.channel, f"{message.author.name}: {message.content}", message.author, message_id=message.id)