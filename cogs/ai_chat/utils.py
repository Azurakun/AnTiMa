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

# Suppress the specific RuntimeWarning from duckduckgo_search regarding 'ddgs' renaming
warnings.filterwarnings("ignore", category=RuntimeWarning, module="duckduckgo_search")

# Robust import for DDGS
try:
    from duckduckgo_search import DDGS
except ImportError:
    try:
        from ddgs import DDGS
    except ImportError:
        DDGS = None

logger = logging.getLogger(__name__)

TENOR_API_KEY = os.environ.get("TENOR_API_KEY")
TENOR_CLIENT_KEY = "AnTiMa-Discord-Bot"

def _safe_get_response_text(response) -> str:
    """Safely gets text from a Gemini response, handling blocked content."""
    try:
        if not response.parts:
            return ""
        text_parts = []
        for part in response.parts:
            if part.text:
                text_parts.append(part.text)
        
        if text_parts:
            return "\n".join(text_parts)
        return ""
    except (ValueError, IndexError, AttributeError):
        logger.warning("Gemini response was empty, blocked, or contained only function calls.")
        return ""

async def get_gif_url(http_session: aiohttp.ClientSession, search_term: str) -> str | None:
    """Fetches a random GIF URL from Tenor based on a search term."""
    if not TENOR_API_KEY:
        logger.warning("TENOR_API_KEY is not set in environment variables. Cannot fetch GIFs.")
        return None

    url = "https://tenor.googleapis.com/v2/search"
    params = {
        "q": search_term,
        "key": TENOR_API_KEY,
        "client_key": TENOR_CLIENT_KEY,
        "limit": 8,
        "media_filter": "minimal",
        "random": "true"
    }

    try:
        async with http_session.get(url, params=params) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("results"):
                    gif = random.choice(data["results"])
                    return gif["media_formats"]["gif"]["url"]
            else:
                logger.error(f"Tenor API request failed with status {response.status}: {await response.text()}")
    except Exception as e:
        logger.error(f"An error occurred while fetching a GIF from Tenor: {e}")
    return None

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
            "1. **APPROVE (yes)** if the conversation is casual, friendly, or emotional where a GIF would enhance the expression (e.g., sharing joy, offering comfort, making a joke).\n"
            "2. **REJECT (no)** if the conversation is serious, technical, formal, or argumentative. A GIF would be inappropriate or distracting.\n"
            "3. **REJECT (no)** if the user seems frustrated or angry. A GIF could escalate the situation unless it's clearly apologetic.\n"
            "4. **REJECT (no)** if the GIF's implied emotion (from the search term) clashes badly with the text response (e.g., text is sad, GIF is 'laughing').\n\n"
            f"--- CONTEXT ---\n"
            f"**Recent Chat:**\n{conversation_log}\n\n"
            f"**My Planned Text Response:**\n\"{bot_response_text}\"\n\n"
            f"**Proposed GIF Search Term:** `{gif_search_term}`\n"
            f"---------------\n\n"
            "Based on your rules, is sending this GIF appropriate right now? Answer with only 'yes' or 'no'."
        )

        response = await summarizer_model.generate_content_async(prompt)
        decision = _safe_get_response_text(response).strip().lower()
        logger.info(f"GIF Decision Agent for search term '{gif_search_term}': '{decision}'")
        return 'yes' in decision

    except Exception as e:
        logger.error(f"GIF Decision Agent failed: {e}")
        return False

def _find_member(guild: discord.Guild, name: str):
    """Finds a member in a guild by name or display name, case-insensitively."""
    name = name.lower()
    member = discord.utils.find(
        lambda m: m.name.lower() == name or m.display_name.lower() == name,
        guild.members
    )
    if member is None:
        logger.warning(f"Could not find member '{name}' in guild '{guild.name}'. The member might not be cached or the name is incorrect.")
    return member

async def fetch_website_content(url: str) -> str:
    """
    Visits a URL and extracts the main text content using BeautifulSoup.
    """
    logger.info(f"DEBUG: Scraper visiting {url}")
    try:
        ua = UserAgent()
        headers = {'User-Agent': ua.random}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=6) as response:
                if response.status != 200:
                    return f"Failed to open link (Status {response.status})"
                
                # Safety: Check content size
                if int(response.headers.get("Content-Length", 0)) > 4_000_000:
                    return "Page too large to read safely."

                html = await response.text()
                
        # Parse HTML
        soup = BeautifulSoup(html, 'html.parser')
        
        # Remove unwanted elements
        for script in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe", "noscript", "svg"]):
            script.extract()
            
        # Strategy: Find paragraphs
        paragraphs = [p.get_text().strip() for p in soup.find_all('p')]
        clean_text = "\n".join([p for p in paragraphs if len(p) > 60])
        
        # Fallback
        if not clean_text or len(clean_text) < 200:
            clean_text = soup.get_text(separator='\n')
        
        # Cleanup
        clean_text = re.sub(r'\n\s*\n', '\n\n', clean_text)
        
        # Truncate
        return clean_text[:3500] + ("..." if len(clean_text) > 3500 else "")

    except asyncio.TimeoutError:
        return "Reading the page timed out."
    except Exception as e:
        logger.error(f"Scraper error on {url}: {e}")
        return f"Could not read page content: {str(e)}"

async def perform_web_search(query: str) -> str:
    """
    Performs a DuckDuckGo search with robust fallback backends and scrapes the content.
    """
    if DDGS is None:
        return "Search tool error: `ddgs` library not found. Please install `ddgs`."

    logger.info(f"DEBUG: AI Search Query: '{query}'")
    loop = asyncio.get_running_loop()

    try:
        # 1. Perform Search with Backend Retry Loop
        def run_ddg_sync():
            results = []
            # We try backends in order. 'api' is fast, 'html' is robust, 'lite' is fallback.
            backends = ['api', 'html', 'lite']
            
            with DDGS() as ddgs:
                for backend in backends:
                    try:
                        logger.info(f"DEBUG: Trying DDG backend: '{backend}'")
                        # Try to get up to 6 results
                        ddgs_gen = ddgs.text(query, max_results=6, backend=backend)
                        
                        if ddgs_gen:
                            # Convert generator to list to ensure we actually have data
                            backend_results = list(ddgs_gen)
                            
                            if backend_results:
                                results = backend_results
                                logger.info(f"DEBUG: Backend '{backend}' succeeded with {len(results)} results.")
                                break # Stop if we found results
                            else:
                                logger.warning(f"DEBUG: Backend '{backend}' returned empty list.")
                        else:
                            logger.warning(f"DEBUG: Backend '{backend}' returned None.")
                            
                    except Exception as backend_error:
                        logger.error(f"DEBUG: Backend '{backend}' failed with error: {backend_error}")
                        continue # Try next backend
            
            return results

        search_results = await loop.run_in_executor(None, run_ddg_sync)
        
        if not search_results:
            return "No results found (All search backends failed)."

        # 2. Format Snippets
        formatted_snippets = "### Search Results Overview:\n"
        for i, r in enumerate(search_results):
            # Handle different dictionary keys from different backends if necessary
            title = r.get('title') or r.get('headline') or "No Title"
            link = r.get('href') or r.get('url') or "No Link"
            body = r.get('body') or r.get('snippet') or "No Snippet"
            formatted_snippets += f"{i+1}. {title} - {link}\n   Snippet: {body}\n"

        # 3. Smart Selection Logic
        priorities = ['wiki', 'fandom', 'hoyolab', 'reddit', 'guide', 'screenrant', 'game8']
        blacklist = ['scmp.com', 'cnn.com', 'bbc.com', 'nytimes.com', 'forbes.com', 'bloomberg.com']

        best_result = None
        
        # Pass 1: Priority
        for r in search_results:
            url_lower = (r.get('href') or r.get('url') or '').lower()
            if any(p in url_lower for p in priorities):
                best_result = r
                logger.info(f"DEBUG: Selected Priority result: {r.get('title')}")
                break
        
        # Pass 2: Non-blacklisted
        if not best_result:
            for r in search_results:
                url_lower = (r.get('href') or r.get('url') or '').lower()
                if not any(b in url_lower for b in blacklist):
                    best_result = r
                    logger.info(f"DEBUG: Selected fallback result: {r.get('title')}")
                    break
        
        # Pass 3: Default
        if not best_result:
            best_result = search_results[0]
            logger.info(f"DEBUG: Defaulted to first result: {best_result.get('title')}")

        # 4. Fetch Content
        target_url = best_result.get('href') or best_result.get('url')
        target_title = best_result.get('title') or "Selected Result"
        
        article_content = await fetch_website_content(target_url)
        
        final_output = (
            f"{formatted_snippets}\n"
            f"### Deep Dive Content (from {target_title}):\n"
            f"{article_content}"
        )
        
        return final_output

    except Exception as e:
        logger.error(f"Search failed: {e}")
        traceback.print_exc()
        return f"Search System Error: {str(e)}"