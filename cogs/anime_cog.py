import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import logging
import datetime
import random
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from utils.danbooru_api import get_random_danbooru_image
from utils.db import anime_gacha_users_collection, anime_gacha_inventory_collection

logger = logging.getLogger(__name__)

# --- GACHA CONFIGURATION ---
PULL_COST = 100
DAILY_REWARD = 1000

# Rarity Thresholds (Fav Count)
RARITY_MAP = [
    (50, "⭐⭐⭐⭐⭐", 0xFFD700, "LEGENDARY"), # Gold
    (20, "⭐⭐⭐⭐", 0x9B59B6, "EPIC"),       # Purple
    (10,  "⭐⭐⭐", 0x3498DB, "RARE"),        # Blue
    (3,   "⭐⭐", 0x2ECC71, "UNCOMMON"),      # Green
    (0,    "⭐", 0x95A5A6, "COMMON")           # Grey
]

def calculate_rarity(fav_count: int):
    for threshold, stars, color, name in RARITY_MAP:
        if fav_count >= threshold:
            return stars, color, name
    return RARITY_MAP[-1][1:] # Fallback



class GachaView(discord.ui.View):
    def __init__(self, user_id: int, image_data: dict, bot):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.image_data = image_data
        self.bot = bot
        self.claimed = False

    @discord.ui.button(label="💞 Claim Waifu/Husbando", style=discord.ButtonStyle.success, emoji="💍")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your pull! Get your own with `/pull`.", ephemeral=True)
            return

        if self.claimed:
            await interaction.response.send_message("You already claimed this!", ephemeral=True)
            return

        # Save to inventory. The unique (user_id, image_id) index rejects duplicates atomically.
        doc = {
            "user_id": interaction.user.id,
            "image_id": self.image_data['id'],
            "character": self.image_data['character'],
            "image_url": self.image_data['image_url'],
            "rarity": self.image_data['rarity_name'],
            "stars": self.image_data['stars'],
            "claimed_at": datetime.datetime.utcnow()
        }
        self.claimed = True
        try:
            await asyncio.to_thread(anime_gacha_inventory_collection.insert_one, doc)
        except DuplicateKeyError:
            # Duplicate mechanic: Convert to coins
            refund = 25
            await asyncio.to_thread(
                anime_gacha_users_collection.update_one,
                {"user_id": interaction.user.id},
                {"$inc": {"credits": refund}}
            )
            await interaction.response.send_message(f"You already own **{self.image_data['character']}**! Converted to {refund} 🪙.", ephemeral=True)
            self.stop()
            return

        button.label = "Claimed!"
        button.disabled = True
        button.style = discord.ButtonStyle.secondary
        
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"🎉 **{self.image_data['character']}** has been added to your collection!")

    @discord.ui.button(label="🗑️ Skip", style=discord.ButtonStyle.danger)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return
        await interaction.response.edit_message(view=None)
        self.stop()

class AnimeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def get_user_profile(self, user_id: int):
        # Upsert so two concurrent first-time commands cannot both create a profile.
        return await asyncio.to_thread(
            anime_gacha_users_collection.find_one_and_update,
            {"user_id": user_id},
            {"$setOnInsert": {"credits": 500, "last_daily": None, "pulls": 0}},  # 500 = starting bonus
            upsert=True,
            return_document=ReturnDocument.AFTER
        )

    @app_commands.command(name="daily", description="Claim your daily gacha credits (1000 🪙)")
    async def daily(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        profile = await self.get_user_profile(user_id)

        now = datetime.datetime.utcnow()
        cooldown = datetime.timedelta(hours=24)

        # Claim only if the cooldown has passed, in one atomic update (prevents double claims).
        updated = await asyncio.to_thread(
            anime_gacha_users_collection.find_one_and_update,
            {"user_id": user_id, "$or": [{"last_daily": None}, {"last_daily": {"$lte": now - cooldown}}]},
            {"$inc": {"credits": DAILY_REWARD}, "$set": {"last_daily": now}},
            return_document=ReturnDocument.AFTER
        )

        if not updated:
            last_daily = profile.get("last_daily") or now
            wait_time = max(datetime.timedelta(0), last_daily + cooldown - now)
            hours, remainder = divmod(int(wait_time.total_seconds()), 3600)
            minutes, _ = divmod(remainder, 60)
            await interaction.response.send_message(f"⏳ Please wait **{hours}h {minutes}m** for your next daily reward.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Daily Reward Claimed!",
            description=f"You received **{DAILY_REWARD}** 🪙 Credits!\nCurrent Balance: **{updated['credits']}** 🪙",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)

    async def execute_pull(self, ctx, gender_tag: str):
        # Handle both Interaction and Context
        user = ctx.author if isinstance(ctx, commands.Context) else ctx.user
        
        # Defer if interaction
        if isinstance(ctx, discord.Interaction):
            await ctx.response.defer()
        
        profile = await self.get_user_profile(user.id)

        # Deduct cost only if the balance covers it, in one atomic update (prevents overspending).
        charged = await asyncio.to_thread(
            anime_gacha_users_collection.find_one_and_update,
            {"user_id": user.id, "credits": {"$gte": PULL_COST}},
            {"$inc": {"credits": -PULL_COST, "pulls": 1}},
            return_document=ReturnDocument.AFTER
        )

        if not charged:
            msg = f"🚫 You need **{PULL_COST}** 🪙 to pull! You have **{profile['credits']}** 🪙.\nUse `/daily` to get more."
            if isinstance(ctx, discord.Interaction):
                await ctx.followup.send(msg, ephemeral=True)
            else:
                await ctx.send(msg)
            return

        # Fetch Image
        result = await get_random_danbooru_image(gender_tag)
        
        if not result or not result.get('image_url'):
            # Refund on failure
            await asyncio.to_thread(anime_gacha_users_collection.update_one, {"user_id": user.id}, {"$inc": {"credits": PULL_COST, "pulls": -1}})
            msg = "⚠️ Failed to find a character. Credits refunded."
            if isinstance(ctx, discord.Interaction):
                await ctx.followup.send(msg, ephemeral=True)
            else:
                await ctx.send(msg)
            return

        # Calculate Rarity
        stars, color, rarity_name = calculate_rarity(result['fav_count'])
        
        # Add metadata for the view
        result['stars'] = stars
        result['rarity_name'] = rarity_name
        
        embed = discord.Embed(
            title=f"{stars} {result['character']} {stars}",
            color=color
        )
        embed.set_image(url=result['image_url'])
        
        # Detailed Metadata Fields
        embed.add_field(name="🎬 Series", value=result.get('series', 'Unknown'), inline=True)
        embed.add_field(name="🎨 Artist", value=result.get('artist', 'Unknown'), inline=True)
        embed.add_field(name="💎 Rarity", value=f"{rarity_name} ({result['fav_count']} ❤️)", inline=True)
        
        embed.set_footer(text=f"Pull Cost: {PULL_COST}🪙 | Remaining: {charged['credits']}🪙")

        view = GachaView(user.id, result, self.bot)
        
        if isinstance(ctx, discord.Interaction):
            await ctx.followup.send(embed=embed, view=view)
        else:
            await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(name="waifu", aliases=["w"], description="Pull a random Waifu (1girl)!")
    async def waifu(self, ctx):
        await self.execute_pull(ctx, "1girl")

    @commands.hybrid_command(name="husbando", aliases=["h"], description="Pull a random Husbando (1boy)!")
    async def husbando(self, ctx):
        await self.execute_pull(ctx, "1boy")

    @app_commands.command(name="profile", description="Check your gacha profile and stats")
    async def profile(self, interaction: discord.Interaction, user: discord.Member = None):
        target_user = user or interaction.user
        profile = await self.get_user_profile(target_user.id)
        
        inventory_count = anime_gacha_inventory_collection.count_documents({"user_id": target_user.id})
        top_card = anime_gacha_inventory_collection.find_one(
            {"user_id": target_user.id},
            sort=[("stars", -1)] 
        )

        embed = discord.Embed(
            title=f"📊 Profile: {target_user.display_name}",
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=target_user.display_avatar.url)
        embed.add_field(name="💰 Credits", value=f"**{profile['credits']}** 🪙", inline=True)
        embed.add_field(name="🃏 Cards Owned", value=f"**{inventory_count}**", inline=True)
        embed.add_field(name="🎰 Total Pulls", value=f"**{profile.get('pulls', 0)}**", inline=True)
        
        if top_card:
            embed.add_field(name="🏆 Rarest Card", value=f"{top_card['stars']} **{top_card['character']}**", inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="inventory", description="View your claimed characters")
    async def inventory(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        cursor = anime_gacha_inventory_collection.find({"user_id": user_id}).sort("claimed_at", -1).limit(10)
        items = list(cursor)

        if not items:
            await interaction.response.send_message("You haven't claimed any characters yet! Use `/pull` to start.", ephemeral=True)
            return

        embed = discord.Embed(title="🎒 Your Latest Acquisitions", color=discord.Color.gold())
        
        description = ""
        for item in items:
            description += f"{item['stars']} **{item['character']}** - *{item['rarity']}*\n"
        
        embed.description = description
        embed.set_footer(text="Showing last 10 items.")
        
        await interaction.response.send_message(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(AnimeCog(bot))