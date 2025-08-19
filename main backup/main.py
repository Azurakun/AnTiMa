import discord
from discord import app_commands
import requests
import os
from flask import Flask
from threading import Thread
import logging
import math
import random
import traceback
import aiohttp

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
                    
    # async def setup_hook(self):
    #     await self.load_tags_from_danbooru()
    #     await self.tree.sync()

    


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



# Helper function to fetch random Danbooru image by tag









async def danbooru_tag_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    if not current:
        return []

    url = f"https://danbooru.donmai.us/autocomplete.json?search[name]={current}&limit=10"
    headers = {"User-Agent": "DiscordBot (by Azura)"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=2)) as response:
                if response.status != 200:
                    return []
                data = await response.json()

                return [
                    app_commands.Choice(name=tag["name"], value=tag["name"])
                    for tag in data
                    if not tag["name"].startswith("rating:")
                ]
    except Exception as e:
        print("[Autocomplete Error]", e)
        traceback.print_exc()
        return []

async def load_tags_from_danbooru(self):
        print("Fetching tags from Danbooru...")
        url = "https://danbooru.donmai.us/tags.json?limit=1000&order=count"
        try:
            async with aiohttp.ClientSession() as session:
                
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.tags = [tag["name"] for tag in data if tag.get("name")]
                        print(f"Loaded {len(self.tags)} tags.")
                    else:
                        print(f"Failed to fetch tags: {resp.status}")
        except Exception as e:
            print("Error fetching tags from Danbooru:", e)


# Define a Discord UI view with button
class AnotherOneButton(discord.ui.View):
    def __init__(self, tags: str, nsfw: bool = False, timeout: int = 60):
        super().__init__(timeout=timeout)
        self.tags = tags
        self.nsfw = nsfw

    @discord.ui.button(label="🔁 Another One!", style=discord.ButtonStyle.primary)
    async def another_one(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            result = get_random_danbooru_image(self.tags, nsfw=self.nsfw)
            if not result:
                await interaction.response.send_message(f"No results found for `{self.tags}`.", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"Here's your `{self.tags or 'Random'}` {'NSFW' if self.nsfw else ''} image!",
                description=f"**Character**: {result['character']}\n**Artist**: {result['artist']}",
                color=discord.Color.red() if self.nsfw else discord.Color.purple()
            )
            embed.set_image(url=result['image_url'])

            if result['source']:
                embed.add_field(name="Source", value=result['source'], inline=False)

            await interaction.response.send_message(embed=embed, view=AnotherOneButton(self.tags, nsfw=self.nsfw))
        except Exception as e:
            logger.error(f"Button error: {e}")
            await interaction.response.send_message("Error fetching new image.", ephemeral=True)


TAGS = ["asuna", "azur_lane", "azusa", "aqua", "aki", "akira"]

@client.tree.command(name="animeimage", description="Fetch a random anime image with artist and character info")
@app_commands.describe(
    tags="Character or tag to search for"
)
@app_commands.autocomplete(tags=danbooru_tag_autocomplete)
@app_commands.describe(tags="Character or tag (autocomplete)")

async def animeimage(interaction: discord.Interaction, tags: str = None):
    try:
        await interaction.response.defer()

        result = get_random_danbooru_image(tags)
        if not result:
            await interaction.followup.send(f"No results found for `{tags}`.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Here's your `{tags or 'Random'}` image!",
            description=f"**Character**: {result['character']}\n**Artist**: {result['artist']}\n**Tag used**: `{result['actual_tag']}`",
            color=discord.Color.purple()
        )
        embed.set_image(url=result['image_url'])

        if result['source']:
            embed.add_field(name="Source", value=result['source'], inline=False)

        view = AnotherOneButton(tags=result['actual_tag'])
        await interaction.followup.send(embed=embed, view=view)

    except Exception as e:
        logger.error(f"Command error: {e}")
        await interaction.followup.send("Oops! Something unexpected went wrong.", ephemeral=True)

def get_danbooru_autocomplete_tag(user_input: str):
    try:
        url = f"https://danbooru.donmai.us/autocomplete.json?search[name]={user_input}&limit=1"
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()

        if data:
            return data[0]['name']
    except Exception as e:
        logger.error(f"Autocomplete tag fetch failed: {e}")

    return user_input.lower().replace(" ", "_")

def get_danbooru_post_count(tag: str) -> int:
    try:
        url = f"https://danbooru.donmai.us/counts/posts.json?tags={tag}+rating:safe"
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        return data.get("counts", {}).get("posts", 0)
    except Exception as e:
        logger.error(f"Post count fetch failed: {e}")
        return 0


def get_random_danbooru_image(tag: str = None, nsfw: bool = False):
    rating_tag = "rating:explicit" if nsfw else "rating:safe"
    actual_tag = get_danbooru_autocomplete_tag(tag) if tag else None
    search_tags = f"{actual_tag}+{rating_tag}" if actual_tag else rating_tag

    total_posts = get_danbooru_post_count(search_tags)
    if total_posts == 0:
        return None

    posts_per_page = 20
    max_page = min(1000, math.ceil(total_posts / posts_per_page))
    random_page = random.randint(1, max_page)

    try:
        url = f"https://danbooru.donmai.us/posts.json?tags={search_tags}&limit={posts_per_page}&page={random_page}"
        response = requests.get(url)
        response.raise_for_status()
        posts = response.json()

        if not posts:
            return None

        post = random.choice(posts)
        return {
            "image_url": post.get("file_url"),
            "character": post.get("tag_string_character", "Unknown Character"),
            "artist": post.get("tag_string_artist", "Unknown Artist"),
            "source": post.get("source", None),
            "actual_tag": actual_tag or "Completely random"
        }
    except Exception as e:
        logger.error(f"Random image fetch failed: {e}")
        return None

























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
        # Convert literal "\n" into real newlines
        message = message.replace("\\n", "\n")
        
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
