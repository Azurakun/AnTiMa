# utils/danbooru_api.py
import aiohttp
import logging
import random
import socket
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) User/AnTiMaBot",
    "Accept": "application/json"
}

DANBOORU_URL = "https://safebooru.org/index.php"

SUGGESTED_TAGS = [
    "1girl", "solo", "long_hair", "smile", "highres", "blush",
    "open_mouth", "blue_eyes", "short_hair", "breasts", "hat",
    "looking_at_viewer", "blonde_hair", "skirt", "thighhighs",
    "touhou", "genshin_impact", "hololive", "azur_lane", "arknights"
]

async def danbooru_tag_autocomplete(current: str) -> list:
    """
    Robust autocomplete using 'tags.json' with proper parameter handling.
    """
    if not current:
        return [{"name": tag, "value": tag} for tag in SUGGESTED_TAGS]

    url = DANBOORU_URL
    
    # Safebooru Autocomplete
    params = {
        "page": "dapi",
        "s": "tag",
        "q": "index",
        "json": "1",
        "name_pattern": f"{current}%",
        "limit": "10",
        "order": "count" 
    }
    
    connector = aiohttp.TCPConnector(ssl=False, family=socket.AF_INET)
    
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, params=params, headers=HEADERS, timeout=10) as response:
                if response.status != 200:
                    return []
                # Safebooru sends text/xml header for JSON sometimes, force parse
                text = await response.text()
                try:
                    data = await response.json(content_type=None)
                    return [{"name": tag["name"], "value": tag["name"]} for tag in data]
                except:
                    # Fallback to XML parsing
                    try:
                        root = ET.fromstring(text)
                        tags = []
                        for tag_elem in root.findall("tag"):
                             name = tag_elem.get("name")
                             if name:
                                 tags.append({"name": name, "value": name})
                        return tags
                    except Exception as e:
                        logger.error(f"Autocomplete XML parse failed: {e}")
                        return []
    except Exception as e:
        logger.error(f"[Autocomplete Error] {e}")
        return []

async def get_post_html(session, post_id):
    """Fetches the HTML page for a specific post to extract detailed tags."""
    url = f"{DANBOORU_URL}?page=post&s=view&id={post_id}"
    try:
        async with session.get(url, headers=HEADERS, timeout=10) as response:
            if response.status == 200:
                return await response.text()
    except Exception as e:
        logger.error(f"Failed to fetch HTML for post {post_id}: {e}")
    return None

async def get_random_danbooru_image(gender_tag: str = "1girl", nsfw: bool = False):
    """
    Fetches a random image from Safebooru (as Danbooru fallback).
    Safebooru does not support 'random=true' well, so we use PID randomization.
    """
    # Safebooru is SFW only.
    if nsfw:
        logger.warning("NSFW requested but Safebooru used (SFW only). Result will be safe.")
    
    # Core tags: gender + solo (to avoid crowds or comics often)
    search_tags = f"{gender_tag}"
    
    connector = aiohttp.TCPConnector(ssl=False, family=socket.AF_INET)
    
    async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
        # STRATEGY 1: Random Page (Safebooru has ~20000 pages for popular queries)
        try:
            random_page = random.randint(0, 100) # 0-indexed
            url = DANBOORU_URL
            params = {
                "page": "dapi",
                "s": "post",
                "q": "index",
                "json": "1",
                "limit": "1",
                "tags": search_tags,
                "pid": str(random_page)
            }
            
            async with session.get(url, params=params, headers=HEADERS, timeout=10) as response:
                if response.status == 200:
                    # Safebooru sometimes sends text/xml content-type for JSON
                    posts = await response.json(content_type=None)
                    if posts:
                        post = posts[0]
                        # Fetch HTML for detailed metadata
                        html = await get_post_html(session, post['id'])
                        return process_post(post, gender_tag, html)
                    else:
                        logger.info("Strategy 1 (Random Page) returned empty. Trying Page 0.")
                else:
                    text = await response.text()
                    logger.warning(f"Strategy 1 failed with {response.status}. Body: {text[:200]}...")

        except Exception as e:
            logger.error(f"Strategy 1 Error: {type(e).__name__}: {e}")

        # STRATEGY 2: Fallback to Page 0
        try:
            params["pid"] = "0"
            async with session.get(url, params=params, headers=HEADERS, timeout=10) as response:
                if response.status == 200:
                    posts = await response.json(content_type=None)
                    if posts:
                        post = posts[0]
                        html = await get_post_html(session, post['id'])
                        return process_post(post, gender_tag, html)
        except Exception as e:
            logger.error(f"Strategy 2 Error: {type(e).__name__}: {e}")

    return None

def process_post(post, gender_tag, html=None):
    """Helper to extract data safely from Safebooru response, optionally using HTML for better metadata."""
    file_url = post.get("file_url")
    
    if not file_url:
        return None

    # Defaults
    character = "Original Character"
    series = "Unknown Series"
    artist = "Unknown Artist"
    score = post.get("score") or 0
    
    if html:
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            # Extract Tags from Sidebar
            tag_sidebar = soup.find('ul', id='tag-sidebar')
            if tag_sidebar:
                artists = []
                copyrights = []
                characters = []
                
                for li in tag_sidebar.find_all('li'):
                    classes = li.get('class', [])
                    
                    # Tag Name Extraction (Safebooru uses standard gelbooru style: <a href="...&tags=tagname">tagname</a>)
                    # Often the second link is the search link.
                    # Text usually works but might include count.
                    links = li.find_all('a')
                    tag_name = None
                    for a in links:
                        # Looking for the search link
                        if 'page=post' in a.get('href', '') and 's=list' in a.get('href', ''):
                            tag_name = a.text.replace("_", " ").title()
                            break
                    
                    if not tag_name: continue

                    if 'tag-type-artist' in classes:
                        artists.append(tag_name)
                    elif 'tag-type-copyright' in classes:
                        copyrights.append(tag_name)
                    elif 'tag-type-character' in classes:
                        characters.append(tag_name)

                if artists: artist = ", ".join(artists[:2]) # Top 2 artists
                if copyrights: series = ", ".join(copyrights[:2]) # Top 2 series
                if characters: character = ", ".join(characters[:2]) # Top 2 characters
            
            # Extract Stats (Score/Favorites) if available in text (harder on Safebooru)
            # Safebooru stats: "Score: 0 (vote up) Favorites: 0"
            stats_div = soup.find('div', id='stats')
            if stats_div:
                text = stats_div.text
                if "Score:" in text:
                    # Very rough parsing
                    try:
                        score_text = text.split("Score:")[1].split()[0]
                        score = int(score_text)
                    except: pass

        except Exception as e:
            logger.error(f"HTML Parsing Error: {e}")

    # Fallback to tag parsing if HTML failed or returned nothing useful
    if character == "Original Character" and not html:
        tags_string = post.get("tags", "")
        tag_list = tags_string.split(" ")
        potential_names = [t for t in tag_list if "(" in t]
        if potential_names:
            first = potential_names[0]
            if "_(" in first:
                parts = first.split("_(")
                character = parts[0].replace("_", " ").title()
                if series == "Unknown Series":
                     series = parts[1].replace(")", "").replace("_", " ").title()

    return {
        "image_url": file_url,
        "character": character,
        "artist": artist,
        "series": series,
        "source": f"https://safebooru.org/index.php?page=post&s=view&id={post.get('id')}",
        "actual_tag": gender_tag,
        "id": post.get("id"),
        "fav_count": score, # Safebooru mainly uses score
        "score": score,
        "rating": post.get("rating", "s")
    }