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

TOKEN = os.environ["TOKEN"]

# Store emoji-role-message mappings per guild: {guild_id: {message_id: {emoji: role_id}}}
emoji_role_map = {}

class AnimeImageBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        intents.reactions = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def on_ready(self):
        await self.wait_until_ready()
        await self.tree.sync()
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info("------")

    async def on_raw_reaction_add(self, payload):
        guild_id = payload.guild_id
        message_id = payload.message_id
        emoji = str(payload.emoji)

        if guild_id in emoji_role_map and message_id in emoji_role_map[guild_id]:
            role_id = emoji_role_map[guild_id][message_id].get(emoji)
            if role_id:
                guild = self.get_guild(guild_id)
                member = guild.get_member(payload.user_id)
                role = guild.get_role(role_id)
                if member and role:
                    await member.add_roles(role)

    async def on_raw_reaction_remove(self, payload):
        guild_id = payload.guild_id
        message_id = payload.message_id
        emoji = str(payload.emoji)

        if guild_id in emoji_role_map and message_id in emoji_role_map[guild_id]:
            role_id = emoji_role_map[guild_id][message_id].get(emoji)
            if role_id:
                guild = self.get_guild(guild_id)
                member = guild.get_member(payload.user_id)
                role = guild.get_role(role_id)
                if member and role:
                    await member.remove_roles(role)

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

# Slash command for admins to set emoji-role pairing on a specific message
@client.tree.command(name="setemojirole", description="Admin command to link an emoji with a role on a specific message")
@app_commands.describe(
    message_id="ID of the message to track reactions on",
    emoji="Emoji to react with",
    role="Role to give when reacted"
)
async def setemojirole(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    try:
        message_id = int(message_id)
    except ValueError:
        await interaction.response.send_message("Invalid message ID format.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    emoji_role_map.setdefault(guild_id, {}).setdefault(message_id, {})[emoji] = role.id
    await interaction.response.send_message(f"Linked emoji {emoji} with role {role.name} on message ID {message_id}.", ephemeral=True)

@client.tree.command(name="msg", description="Send a message (with optional mentions and embed) to a specific channel")
@app_commands.describe(
    channel_id="The ID of the channel to send the message to",
    message="The message content (plain text or embed description)",
    mention_user="User to mention (optional)",
    mention_role="Role to mention (optional)",
    embed_title="Embed title (optional)",
    embed_color="Embed color in HEX, e.g. #ff5733 (optional)"
)
async def send_message(
    interaction: discord.Interaction,
    channel_id: str,
    message: str,
    mention_user: discord.User = None,
    mention_role: discord.Role = None,
    embed_title: str = None,
    embed_color: str = None
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    try:
        channel = client.get_channel(int(channel_id))
        if channel is None:
            await interaction.response.send_message("Invalid channel ID.", ephemeral=True)
            return

        # Build message content with mentions
        mention_text = ""
        if mention_user:
            mention_text += mention_user.mention + " "
        if mention_role:
            mention_text += mention_role.mention + " "

        # If embed is specified
        if embed_title or embed_color:
            color = discord.Color.default()
            if embed_color:
                try:
                    color = discord.Color(int(embed_color.lstrip("#"), 16))
                except ValueError:
                    await interaction.response.send_message("Invalid color format. Use HEX like #ff5733.", ephemeral=True)
                    return

            embed = discord.Embed(title=embed_title or "Message", description=message, color=color)
            await channel.send(content=mention_text or None, embed=embed)
        else:
            await channel.send(content=f"{mention_text}{message}".strip())

        await interaction.response.send_message(f"Message sent to <#{channel_id}>.", ephemeral=True)

    except Exception as e:
        logger.error(f"Error sending message: {e}")
        await interaction.response.send_message("Failed to send the message. Make sure the bot has access to the channel.", ephemeral=True)


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
