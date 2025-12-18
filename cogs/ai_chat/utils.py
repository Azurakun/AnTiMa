# cogs/ai_chat/utils.py
import discord
import logging
import os
import random
import aiohttp
import asyncio
import functools
import traceback
import warnings
import re
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
import google.generativeai as genai

# Suppress DuckDuckGo warnings
warnings.filterwarnings("ignore", category=RuntimeWarning, module="duckduckgo_search")

try:
    from duckduckgo_search import DDGS
except ImportError:
    try:
        from ddgs import DDGS
    except ImportError:
        DDGS = None

logger = logging.getLogger(__name__)

# Constants for APIs
TENOR_API_KEY = os.environ.get("TENOR_API_KEY")
TENOR_CLIENT_KEY = "AnTiMa-Discord-Bot"
GOOGLE_SEARCH_API_KEY = os.environ.get("GOOGLE_SEARCH_API_KEY")
GOOGLE_SEARCH_CX = os.environ.get("GOOGLE_SEARCH_CX")

def _safe_get_response_text(response) -> str:
    """Safely gets text from a Gemini response, handling blocked content."""
    try:
        if not response.parts:
            return ""
        text_parts = [part.text for part in response.parts if part.text]
        return "\n".join(text_parts) if text_parts else ""
    except (ValueError, IndexError, AttributeError):
        return ""

async def fetch_website_content(url: str) -> str:
    """Visits a URL and extracts the main text content with high precision."""
    try:
        ua = UserAgent()
        headers = {'User-Agent': ua.random}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=8) as response:
                if response.status != 200:
                    return f"Failed to open link (Status {response.status})"
                
                if int(response.headers.get("Content-Length", 0)) > 5_000_000:
                    return "Page too large to read safely."

                html = await response.text()
                
        soup = BeautifulSoup(html, 'html.parser')
        
        # Clean the DOM
        for element in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe", "noscript", "svg"]):
            element.extract()
            
        # Prioritize main content areas
        main_content = soup.find('main') or soup.find('article') or soup.find('div', class_=re.compile(r'content|article|body', re.I))
        target = main_content if main_content else soup
        
        paragraphs = [p.get_text().strip() for p in target.find_all('p') if len(p.get_text().strip()) > 40]
        clean_text = "\n\n".join(paragraphs)
        
        if not clean_text or len(clean_text) < 300:
            clean_text = target.get_text(separator='\n', strip=True)
        
        # Cleanup whitespace
        clean_text = re.sub(r'\n\s*\n', '\n\n', clean_text)
        return clean_text[:4000] + ("..." if len(clean_text) > 4000 else "")

    except Exception as e:
        logger.error(f"Scraper error on {url}: {e}")
        return f"Could not read page: {str(e)}"

async def _get_google_search_results(query: str) -> list:
    """Fetches raw results from Google Custom Search API."""
    if not GOOGLE_SEARCH_API_KEY or not GOOGLE_SEARCH_CX:
        logger.warning("Google Search API credentials missing.")
        return []

    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        'q': query,
        'key': GOOGLE_SEARCH_API_KEY,
        'cx': GOOGLE_SEARCH_CX,
        'num': 5
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('items', [])
                logger.error(f"Google Search API error: {resp.status}")
                return []
    except Exception as e:
        logger.error(f"Google Search request failed: {e}")
        return []

async def _get_ddg_search_results(query: str) -> list:
    """Fallback search using DuckDuckGo."""
    if DDGS is None: return []
    loop = asyncio.get_running_loop()
    try:
        def run_ddg():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=5))
        return await loop.run_in_executor(None, run_ddg)
    except:
        return []

async def perform_web_search(query: str) -> str:
    """
    Advanced search tool that uses Google, scrapes multiple sources, 
    and verifies information accuracy before returning results.
    """
    logger.info(f"Initiating verified search for: '{query}'")
    
    results = await _get_google_search_results(query)
    source_engine = "Google"
    if not results:
        results = await _get_ddg_search_results(query)
        source_engine = "DuckDuckGo"

    if not results:
        return "Search failed: No results found on Google or DuckDuckGo."

    search_data = []
    fetch_tasks = []
    
    for i, r in enumerate(results[:3]):
        title = r.get('title') or r.get('headline', 'No Title')
        link = r.get('link') or r.get('href', 'No Link')
        snippet = r.get('snippet') or r.get('body', '')
        search_data.append({"title": title, "link": link, "snippet": snippet})
        fetch_tasks.append(fetch_website_content(link))

    scraped_contents = await asyncio.gather(*fetch_tasks)
    
    try:
        verification_model = genai.GenerativeModel('gemini-2.5-flash')
        context_blob = ""
        for i, data in enumerate(search_data):
            context_blob += f"SOURCE {i+1} [{data['title']}]:\n{scraped_contents[i]}\n---\n"

        verify_prompt = (
            f"You are a fact-checking module for AnTiMa. Based on the following search data from {source_engine}, "
            f"provide a highly accurate, verified, and detailed answer to the query: '{query}'.\n\n"
            "INSTRUCTIONS:\n"
            "1. Cross-reference the sources. If they conflict, highlight the most reliable one (wikis/official sites).\n"
            "2. Remove any irrelevant SEO fluff or ads.\n"
            "3. Ensure the information is directly related to the user's intent.\n"
            "4. Include key details and specific facts.\n\n"
            f"DATA:\n{context_blob}"
        )

        response = await verification_model.generate_content_async(verify_prompt)
        final_info = _safe_get_response_text(response)
        
        if not final_info:
            final_info = "Verification failed, falling back to raw snippets.\n" + "\n".join([f"- {d['title']}: {d['snippet']}" for d in search_data])

        return f"### VERIFIED INFORMATION (via {source_engine}):\n{final_info}\n\nSources explored: " + ", ".join([d['link'] for d in search_data])

    except Exception as e:
        logger.error(f"Verification step failed: {e}")
        return "Internal error during information verification."

async def identify_visual_content(visual_description: str) -> str:
    """Identifies visual content by performing a targeted search for origin/source."""
    search_query = f"what is {visual_description} character series name origin wiki"
    return await perform_web_search(search_query)

async def should_send_gif(summarizer_model, channel, bot_response_text, gif_search_term) -> bool:
    """Uses an AI agent to determine if sending a GIF is appropriate for the context."""
    try:
        history = [msg async for msg in channel.history(limit=5)]
        history.reverse()

        conversation_log = "\n".join([f"{msg.author.display_name}: {msg.clean_content}" for msg in history])

        prompt = (
            "You are a social context analysis AI. Your job is to decide if sending a GIF is appropriate for the current conversation mood. "
            "I will provide the recent chat history, my planned text response, and the GIF I want to send (as a search term).\n\n"
            "**Rules for your decision:**\n"
            "1. **APPROVE (yes)** if the conversation is casual, friendly, or emotional where a GIF would enhance the expression.\n"
            "2. **REJECT (no)** if the conversation is serious, technical, formal, or argumentative.\n"
            "3. **REJECT (no)** if the user seems frustrated or angry.\n"
            "4. **REJECT (no)** if the GIF's implied emotion clashes with the text response.\n\n"
            f"--- CONTEXT ---\n"
            f"**Recent Chat:**\n{conversation_log}\n\n"
            f"**My Planned Text Response:**\n\"{bot_response_text}\"\n\n"
            f"**Proposed GIF Search Term:** `{gif_search_term}`\n"
            "Answer with only 'yes' or 'no'."
        )

        response = await summarizer_model.generate_content_async(prompt)
        decision = _safe_get_response_text(response).strip().lower()
        logger.info(f"GIF Decision Agent for search term '{gif_search_term}': '{decision}'")
        return 'yes' in decision

    except Exception as e:
        logger.error(f"GIF Decision Agent failed: {e}")
        return False

async def get_gif_url(http_session: aiohttp.ClientSession, search_term: str) -> str | None:
    if not TENOR_API_KEY: return None
    url = "https://tenor.googleapis.com/v2/search"
    params = {"q": search_term, "key": TENOR_API_KEY, "client_key": TENOR_CLIENT_KEY, "limit": 8, "random": "true"}
    try:
        async with http_session.get(url, params=params) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("results"):
                    return random.choice(data["results"])["media_formats"]["gif"]["url"]
    except: pass
    return None

def _find_member(guild: discord.Guild, name: str):
    name = name.lower()
    return discord.utils.find(lambda m: m.name.lower() == name or m.display_name.lower() == name, guild.members)