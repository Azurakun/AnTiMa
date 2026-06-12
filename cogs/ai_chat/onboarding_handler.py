# cogs/ai_chat/onboarding_handler.py
import discord
import logging
from datetime import datetime, timezone
import asyncio
import json
import re

from utils.db import onboarding_sessions_collection, ai_config_collection
from utils.ai_client import get_client, MAIN_MODEL, throttled_create
from .prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# --- SYSTEM PROMPT ---
BASE_ONBOARDING_INSTRUCTIONS = """
### ONBOARDING MISSION
You are currently in "Onboarding Mode". 
Your goal is to interview this new user to assign them roles.

### EXPLAINING "WHY"
If the user asks WHY or refuses a REQUIRED role:
"I need to know this to ensure you are a real human and to give you access! **You won't be able to see ANY channels** without these roles. It's a security step! ( ◡‿◡ *)"

### REQUIRED INFORMATION & RULES
{requirements_text}

### CRITICAL RULES
1. **STRICT ENFORCEMENT (ZERO TOLERANCE)**: 
    - If a role is **REQUIRED**, you **CANNOT** skip it.
    - If they refuse: "I'm sorry, but I **cannot** let you into the server without this. It's required for verification! please help me out? <3"
    - **NEVER** say "it's okay" or "we can skip" for a REQUIRED role.
    - If they persist in refusing, stop asking other questions and just repeat that this is necessary for access.
2. **MISSION FOCUS (NO DISTRACTIONS)**:
    - **DO NOT** talk about games, movies, or hobbies unless it's strictly about assigning a role (e.g. "Do you play D&D?").
    - If the user tries to chat about off-topic things, politely redirect: "That sounds fun! But let's finish setting up your roles first so you can chat in the main channels! ( ◡‿◡ *)"
3. **One thing at a time**: Don't dump all questions. Chat naturally.
4. **Split Messages**: Use `|||` to separate thoughts.
5. **Finalize**: When ALL required info is gathered, call `submit_onboarding_summary`.
    - Provide a concise summary of what you learned.
    - List the EXACT role keys you decided on.
"""

DEFAULT_REQ = "- No specific requirements by default. Just chat and assign relevant roles."
ONBOARDING_SYSTEM_PROMPT = SYSTEM_PROMPT + "\n" + BASE_ONBOARDING_INSTRUCTIONS.format(requirements_text=DEFAULT_REQ)

# --- TOOL SCHEMA (OpenAI format) ---
ONBOARDING_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "submit_onboarding_summary",
            "description": "Call this when you have gathered all necessary information from the user to assign their roles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "A brief friendly summary of what the user told you."
                    },
                    "roles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The list of role keys to assign (e.g. ['nsfw', 'indonesia'])."
                    }
                },
                "required": ["summary", "roles"]
            }
        }
    }
]

# --- VIEWS ---
class OnboardingReviewView(discord.ui.View):
    def __init__(self, handler, message, roles, role_map):
        super().__init__(timeout=None)
        self.handler = handler
        self.message = message
        self.roles = roles
        self.role_map = role_map

    @discord.ui.button(label="✅ Confirm & Enter", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

        added_mentions = await self.handler._apply_roles(interaction.guild, interaction.user, self.roles, self.role_map)

        final_msg = (
            f"🎉 **You're all set!**\n"
            f"Access Granted: {', '.join(added_mentions) if added_mentions else 'Basic Access'}\n\n"
            "Enjoy the server! I'll close this thread now."
        )
        await interaction.followup.send(final_msg)

        session = onboarding_sessions_collection.find_one({"thread_id": interaction.channel.id})
        await self.handler._finish_session(interaction.channel, session)


# --- HANDLER ---
class OnboardingHandler:
    def __init__(self, bot, model_unused=None):
        self.bot = bot
        self.sessions = {}

    async def handle_message(self, message: discord.Message):
        """Processes a message in an onboarding thread."""
        thread_id = message.channel.id

        session = onboarding_sessions_collection.find_one({"thread_id": thread_id, "status": "active"})
        if not session:
            return

        config = ai_config_collection.find_one({"_id": str(message.guild.id)}) or {}
        role_map = config.get("onboarding_roles", {})

        # Build dynamic requirements text
        req_text = ""
        for key, data in role_map.items():
            if isinstance(data, dict):
                req = data.get("req", "optional")
                req_str = "REQUIRED (MUST ANSWER/PICK ONE)" if req in ["required", "one_of"] else "OPTIONAL"
                req_text += f"- **{key.upper()}**: {req_str}\n"
            else:
                req_text += f"- **{key.upper()}**: OPTIONAL\n"

        if not req_text:
            req_text = "No specific requirements. Just chat and assign relevant roles."

        system_content = (
            SYSTEM_PROMPT + "\n" +
            BASE_ONBOARDING_INSTRUCTIONS.format(requirements_text=req_text)
        )

        # Build history from thread
        history_msgs = []
        async for msg in message.channel.history(limit=20):
            if not msg.clean_content:
                continue
            role = "assistant" if msg.author == self.bot.user else "user"
            history_msgs.append({"role": role, "content": msg.clean_content})
        history_msgs.reverse()

        # Build full messages list: system + history + current context injection
        context_injection = (
            f"[SYSTEM: ONBOARDING MODE ACTIVE]\n"
            f"Current Guild Role Requirements:\n{req_text}\n\n"
            "Remember: STRICTLY enforce REQUIRED fields. Stay FOCUSED on the task.\n"
            "Analyze state. Chat naturally. Call submit_onboarding_summary if all info is confirmed."
        )

        messages = [
            {"role": "system", "content": system_content},
            *history_msgs,
            {"role": "user", "content": context_injection},
        ]

        async with message.channel.typing():
            client = get_client()
            response = await throttled_create(client.chat.completions.create(
                model=MAIN_MODEL,
                messages=messages,
                tools=ONBOARDING_TOOLS,
                tool_choice="auto",
            ))

            msg_obj = response.choices[0].message

            # Check for tool call first
            if msg_obj.tool_calls:
                for tc in msg_obj.tool_calls:
                    if tc.function.name == "submit_onboarding_summary":
                        try:
                            args = json.loads(tc.function.arguments)
                        except Exception:
                            args = {}
                        summary = args.get("summary", "Ready to join!")
                        roles = args.get("roles", [])

                        embed = discord.Embed(
                            title="📝 Onboarding Summary",
                            description=summary,
                            color=discord.Color.gold()
                        )
                        embed.add_field(
                            name="🔑 Roles to Assign",
                            value=", ".join(roles) if roles else "None",
                            inline=False
                        )
                        embed.set_footer(text="Click below to confirm and join!")

                        view = OnboardingReviewView(self, message, roles, role_map)
                        await message.channel.send(embed=embed, view=view)
                        return  # View handles the rest

            # Send text response
            response_text = msg_obj.content or ""
            if response_text:
                await self._send_split_message(message.channel, response_text, message.guild)

    async def _send_split_message(self, channel, text, guild):
        """Splits message by ||| and sends sequentially with typing delays."""
        def resolve_mention(match):
            name = match.group(1).strip()
            member = discord.utils.find(
                lambda m: m.name.lower() == name.lower() or m.display_name.lower() == name.lower(),
                guild.members
            )
            return member.mention if member else name

        processed_text = re.sub(r"\[MENTION: (.+?)\]", resolve_mention, text)

        parts = processed_text.split('|||')
        for part in parts:
            part = part.strip()
            if not part:
                continue
            async with channel.typing():
                await asyncio.sleep(len(part) * 0.03)
                await channel.send(part)

    async def _apply_roles(self, guild, member, role_keys, role_map):
        """Helper to actually give roles. Returns list of mentions."""
        default_map = {
            "nsfw": "NSFW",
            "indonesian": "Indonesians",
            "malaysian": "Malaysians",
            "overseas": "Overseas Friends",
            "dnd": "DnD Players",
            "dm": "Dungeon Master"
        }

        to_add = []

        for key in role_keys:
            key = key.lower()
            role_identifier = role_map.get(key)
            target_role = None

            if role_identifier:
                if isinstance(role_identifier, dict):
                    rid = role_identifier.get("id")
                    if rid:
                        target_role = guild.get_role(int(rid))
                elif isinstance(role_identifier, int) or str(role_identifier).isdigit():
                    target_role = guild.get_role(int(role_identifier))
                else:
                    target_role = discord.utils.get(guild.roles, name=str(role_identifier))

            if not target_role and key in default_map:
                target_role = discord.utils.get(guild.roles, name=default_map[key])

            if target_role:
                to_add.append(target_role)
            else:
                logger.warning(f"Onboarding: Could not find role for key '{key}'")

        added_mentions = []
        if to_add:
            try:
                await member.add_roles(*to_add, reason="AI Onboarding")
                added_mentions = [role.mention for role in to_add]
            except Exception as e:
                logger.error(f"Failed to assign roles: {e}")

        return added_mentions

    async def _finish_session(self, thread, session):
        """Closes the thread and updates DB."""
        if not session:
            return

        await asyncio.sleep(4)

        onboarding_sessions_collection.update_one(
            {"_id": session["_id"]},
            {"$set": {"status": "completed", "completed_at": datetime.now(timezone.utc)}}
        )

        try:
            await thread.edit(archived=True, locked=True, reason="Onboarding Complete")
        except Exception as e:
            logger.error(f"Failed to close thread {thread.id}: {e}")
