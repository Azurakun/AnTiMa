# cogs/ai_chat/utils.py
import discord
import logging
import os
import random
import aiohttp
import asyncio
import functools
import re
import warnings
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
import google.generativeai as genai
from datetime import datetime

# Import database collections for logging
from utils.db import search_debug_collection 

warnings.filterwarnings("ignore", category=RuntimeWarning, module="duckduckgo_search")

try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None

logger = logging.getLogger(__name__)

TENOR_API_KEY = os.environ.get("TENOR_API_KEY")
GOOGLE_SEARCH_API_KEY = os.environ.get("GOOGLE_SEARCH_API_KEY")
GOOGLE_SEARCH_ENGINE_ID = os.environ.get("GOOGLE_SEARCH_ENGINE_ID")
TENOR_CLIENT_KEY = "AnTiMa-Discord-Bot"

def _safe_get_response_text(response) -> str:
    try:
        if not response.parts: return ""
        return "\n".join([part.text for part in response.parts if part.text])
    except: return ""

async def fetch_website_content(url: str) -> str:
    """Enhanced scraper with better noise reduction and higher content limits."""
    try:
        ua = UserAgent()
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={'User-Agent': ua.random}, timeout=12) as resp:
                if resp.status != 200: return f"Error {resp.status}"
                html = await resp.text()
                
        soup = BeautifulSoup(html, 'html.parser')
        for s in soup(["script", "style", "nav", "footer", "header", "aside", "form", "ad"]): s.extract()
        
        main = soup.find('main') or soup.find('article') or soup.find('div', class_=re.compile(r'content|body|main', re.I))
        target = main if main else soup
        
        texts = [t.get_text().strip() for t in target.find_all(['p', 'h1', 'h2', 'h3', 'li']) if len(t.get_text().strip()) > 30]
        clean_text = "\n\n".join(list(dict.fromkeys(texts)))
        return clean_text[:6000] # Increased context per source
    except Exception as e:
        return f"Scrape failed: {str(e)}"

async def google_custom_search(query: str, num_results: int = 5):
    """
    Performs a Google Custom Search.
    Returns a list of dicts with 'title', 'link', 'snippet'.
    """
    if not GOOGLE_SEARCH_API_KEY or not GOOGLE_SEARCH_ENGINE_ID:
        return None

    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        'key': GOOGLE_SEARCH_API_KEY,
        'cx': GOOGLE_SEARCH_ENGINE_ID,
        'q': query,
        'num': num_results
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get('items', [])
                return [
                    {
                        'title': item.get('title'),
                        'link': item.get('link'),
                        'snippet': item.get('snippet')
                    }
                    for item in items
                ]
            else:
                logger.error(f"Google Search failed: {resp.status} - {await resp.text()}")
                return None

async def perform_web_search(query: str) -> str:
    """
    REQUIRED TOOL: Searches the internet to find real-time information, facts, or news.
    USE THIS WHENEVER:
    1. The user asks about current events, games, tech, or specific facts.
    2. You are unsure about an answer.
    3. You need to verify something.
    
    Args:
        query: The search string (e.g. "latest Elden Ring patch notes").
    """
    start_time = datetime.utcnow()
    logger.info(f"Deep Search Initiated: {query}")
    
    raw_results = None
    
    # 1. Try Google Search First
    if GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_ENGINE_ID:
        try:
            raw_results = await google_custom_search(query, num_results=10)
        except Exception as e:
            logger.error(f"Google Search Exception: {e}")
    
    # 2. Fallback to DuckDuckGo if Google fails or is unconfigured
    if not raw_results:
        if not DDGS: return "Search disabled: Missing library and no Google keys."
        try:
            loop = asyncio.get_running_loop()
            def run_ddg():
                with DDGS() as ddgs:
                    return list(ddgs.text(query, max_results=15))
            raw_results = await loop.run_in_executor(None, run_ddg)
            # DDG results usually have 'href' and 'body', normalize them
            for r in raw_results:
                if 'href' in r and 'link' not in r: r['link'] = r['href']
                if 'body' in r and 'snippet' not in r: r['snippet'] = r['body']
        except Exception as e:
            return f"Search Error: {e}"

    if not raw_results: return "No results found for that query."

    # 2. Parallel Deep Scrape (Top 8-10 sources)
    search_data = []
    fetch_tasks = []
    
    # Filter and prioritize results
    for r in raw_results[:8]:
        link = r.get('link') or r.get('href')
        if not link: continue
        search_data.append({"title": r.get('title'), "link": link, "snippet": r.get('snippet') or r.get('body')})
        fetch_tasks.append(fetch_website_content(link))

    scraped_contents = await asyncio.gather(*fetch_tasks)
    
    # 3. High-Accuracy Synthesis
    try:
        verification_model = genai.GenerativeModel('gemini-2.5-flash')
        context_blob = ""
        for i, (data, content) in enumerate(zip(search_data, scraped_contents)):
            context_blob += f"SOURCE {i+1} [{data['title']}]:\n{content}\n---\n"

        verify_prompt = (
            f"You are the Ultimate Truth Engine. Analyze the data below to answer: '{query}'.\n\n"
            "INSTRUCTIONS:\n"
            "1. CROSS-REFERENCE: Use all available sources. Prioritize official wikis and news.\n"
            "2. MAXIMUM DETAIL: Provide a deep, comprehensive answer. Do not skip nuances.\n"
            "3. NO UNCERTAINTY: Do not say 'I don't know' if any source has info. Be confident.\n"
            f"DATA:\n{context_blob}"
        )

        response = await verification_model.generate_content_async(verify_prompt)
        final_info = _safe_get_response_text(response)
        
        # 4. LOG TO DEBUG (For Dashboard)
        debug_entry = {
            "query": query,
            "timestamp": start_time,
            "source_count": len(search_data),
            "sources": search_data,
            "synthesis": final_info,
            "processing_time": (datetime.utcnow() - start_time).total_seconds()
        }
        try:
            search_debug_collection.insert_one(debug_entry)
        except: pass

        return f"### VERIFIED SEARCH RESULTS:\n{final_info}\n\nSources used: " + ", ".join([d['link'] for d in search_data[:5]]) + " (+ more)"

    except Exception as e:
        logger.error(f"Synthesis failed: {e}")
        return "Internal synthesis error during verification."

async def identify_visual_content(visual_description: str) -> str:
    return await perform_web_search(f"exact name and series origin of {visual_description} wiki")

async def should_send_gif(summarizer_model, channel, bot_response_text, gif_search_term) -> bool:
    try:
        history = [msg async for msg in channel.history(limit=5)]
        prompt = f"Context: {[m.clean_content for m in history]}\nResponse: {bot_response_text}\nGIF: {gif_search_term}\nAppropriate? yes/no"
        res = await summarizer_model.generate_content_async(prompt)
        return 'yes' in _safe_get_response_text(res).lower()
    except: return False

async def get_gif_url(http_session: aiohttp.ClientSession, search_term: str) -> str | None:
    if not TENOR_API_KEY: return None
    params = {"q": search_term, "key": TENOR_API_KEY, "client_key": TENOR_CLIENT_KEY, "limit": 1, "random": "true"}
    try:
        async with http_session.get("https://tenor.googleapis.com/v2/search", params=params) as resp:
            data = await resp.json()
            return data["results"][0]["media_formats"]["gif"]["url"]
    except: return None

def _find_member(guild: discord.Guild, name: str):
    return discord.utils.find(lambda m: m.name.lower() == name.lower() or m.display_name.lower() == name.lower(), guild.members)