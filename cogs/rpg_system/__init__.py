# cogs/rpg_system/__init__.py
from .cog import RPGAdventureCog

async def setup(bot):
    await bot.add_cog(RPGAdventureCog(bot))