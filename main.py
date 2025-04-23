import discord
from discord import app_commands
import requests
import os
from flask import Flask
from threading import Thread
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    logger.error("TOKEN environment variable is not set. Please set it before running the bot.")

# Store emoji-role mappings per guild
emoji_role_map = {}  # {guild_id: {emoji: role_id}}

class AnimeImageBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True  # Make sure this intent is enabled in the Discord developer portal as well
        intents.reactions = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def on_ready(self):
        try:
            await self.tree.sync()
            logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
            logger.info("------")
        except Exception as e:
            logger.error(f"Error during bot startup: {e}")

    async def on_raw_reaction_add(self, payload):
        guild = self.get_guild(payload.guild_id)
        if not guild:
            logger.warning(f"Guild not found for guild_id: {payload.guild_id}")
            return

        member = payload.member
        if member is None:
            member = guild.get_member(payload.user_id)
            if member is None:
                logger.warning(f"Member not found for user_id: {payload.user_id} in guild_id: {payload.guild_id}")
                return

        if member.bot:
            return

        emoji = str(payload.emoji)
        role_id = emoji_role_map.get(payload.guild_id, {}).get(emoji)
        if role_id:
            role = guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role)
                except Exception as e:
                    logger.error(f"Failed to add role {role.name} to member {member.display_name}: {e}")

    async def on_raw_reaction_remove(self, payload):
        guild = self.get_guild(payload.guild_id)
        if not guild:
            logger.warning(f"Guild not found for guild_id: {payload.guild_id}")
            return

        member = guild.get_member(payload.user_id)
        if member is None:
            logger.warning(f"Member not found for user_id: {payload.user_id} in guild_id: {payload.guild_id}")
            return

        if member.bot:
            return

        emoji = str(payload.emoji)
        role_id = emoji_role_map.get(payload.guild_id, {}).get(emoji)
        if role_id:
            role = guild.get_role(role_id)
            if role:
                try:
                    await member.remove_roles(role)
                except Exception as e:
                    logger.error(f"Failed to remove role {role.name} from member {member.display_name}: {e}")

client = AnimeImageBot()

# Slash command to fetch anime image
@client.tree.command(name="animeimage", description="Fetch a random anime image")
@app_commands.describe(
    category="Choose a category like 'sfw' or 'nsfw'",
    type="Type of image, e.g., waifu, neko, shinobu, bully, etc."
)
async def animeimage(interaction: discord.Interaction, category: str = "sfw", type: str = "waifu"):
    try:
        url = f"https://api.waifu.pics/{category}/{type}"
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        image_url = data["url"]

        embed = discord.Embed(title=f"Here's your random {type}!", color=discord.Color.purple())
        embed.set_image(url=image_url)

        await interaction.response.send_message(embed=embed)
    except requests.RequestException as e:
        logger.error(f"API request error: {e}")
        error_message = f"Error: Failed to fetch image from the API. Status code: {e.response.status_code if hasattr(e, 'response') else 'N/A'}"
        await interaction.response.send_message(error_message)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await interaction.response.send_message("Oops! Something unexpected went wrong.")

# Slash command for admins to set emoji-role pairing
@client.tree.command(name="setemojirole", description="Admin command to link an emoji with a role")
@app_commands.describe(
    emoji="Emoji to react with",
    role="Role to give when reacted"
)
async def setemojirole(interaction: discord.Interaction, emoji: str, role: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    emoji_role_map.setdefault(guild_id, {})[emoji] = role.id
    await interaction.response.send_message(f"Linked emoji {emoji} with role {role.name}.", ephemeral=True)

# Flask server for uptime
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    try:
        app.run(host='0.0.0.0', port=5000)
    except Exception as e:
        logger.error(f"Flask server error: {e}")

def main():
    try:
        Thread(target=run).start()
        client.run(TOKEN)
    except Exception as e:
        logger.error(f"Main execution error: {e}")

if __name__ == "__main__":
    main()
