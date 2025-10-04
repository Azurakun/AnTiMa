# utils/danbooru_api.py
import aiohttp
import random
import math
import logging
import traceback

logger = logging.getLogger(__name__)

# --- NEW: Predefined list of popular tags for autocomplete ---
SUGGESTED_TAGS = [
    "1girl", "solo", "long_hair", "smile", "highres", "blush",
    "open_mouth", "blue_eyes", "short_hair", "breasts", "hat",
    "looking_at_viewer", "blonde_hair", "skirt", "thighhighs",
    "touhou", "genshin_impact", "hololive", "azur_lane", "arknights"
]

async def danbooru_tag_autocomplete(current: str) -> list:
    """
    Provides autocomplete suggestions for Danbooru tags.
    If 'current' is empty, it returns a predefined list of popular tags.
    Otherwise, it queries the Danbooru API for matching tags.
    """
    # If the user hasn't typed anything, show the suggested tags
    if not current:
        return [{"name": tag, "value": tag} for tag in SUGGESTED_TAGS]

    # If the user is typing, fetch live suggestions from the API
    url = f"https://danbooru.donmai.us/autocomplete.json?search[name]={current}&limit=10"
    headers = {"User-Agent": "DiscordBot (by Azura)"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=2)) as response:
                if response.status != 200:
                    return []
                data = await response.json()
                return data
    except Exception as e:
        logger.error(f"[Autocomplete Error] {e}")
        traceback.print_exc()
        return []

async def get_danbooru_autocomplete_tag(session, user_input: str):
    try:
        url = f"https://danbooru.donmai.us/autocomplete.json?search[name]={user_input}&limit=1"
        async with session.get(url) as response:
            response.raise_for_status()
            data = await response.json()
            if data:
                return data[0]['name']
    except Exception as e:
        logger.error(f"Autocomplete tag fetch failed: {e}")
    return user_input.lower().replace(" ", "_")

async def get_danbooru_post_count(session, tag: str) -> int:
    try:
        url = f"https://danbooru.donmai.us/counts/posts.json?tags={tag}"
        async with session.get(url) as response:
            response.raise_for_status()
            data = await response.json()
            return data.get("counts", {}).get("posts", 0)
    except Exception as e:
        logger.error(f"Post count fetch failed: {e}")
        return 0

async def get_random_danbooru_image(tag: str = None, nsfw: bool = False):
    rating_tag = "rating:explicit" if nsfw else "rating:safe"
    
    async with aiohttp.ClientSession() as session:
        actual_tag = await get_danbooru_autocomplete_tag(session, tag) if tag else None
        search_tags = f"{actual_tag}+{rating_tag}" if actual_tag else rating_tag

        total_posts = await get_danbooru_post_count(session, search_tags)
        if total_posts == 0:
            return None

        posts_per_page = 20
        max_page = min(1000, math.ceil(total_posts / posts_per_page))
        random_page = random.randint(1, max_page)

        try:
            url = f"https://danbooru.donmai.us/posts.json?tags={search_tags}&limit={posts_per_page}&page={random_page}"
            async with session.get(url) as response:
                response.raise_for_status()
                posts = await response.json()

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