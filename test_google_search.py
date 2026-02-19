import asyncio
import os
import logging
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SearchTest")

# Import the function to test
try:
    from cogs.ai_chat.utils import perform_web_search
except ImportError:
    # If running from root directory
    import sys
    sys.path.append(os.getcwd())
    from cogs.ai_chat.utils import perform_web_search

async def test_search():
    query = "latest Python version release date"
    print(f"Testing search with query: '{query}'")
    
    # Check if keys are present
    google_key = os.environ.get("GOOGLE_SEARCH_API_KEY")
    engine_id = os.environ.get("GOOGLE_SEARCH_ENGINE_ID")
    
    if google_key and engine_id:
        print("✅ Google Search Keys found.")
    else:
        print("⚠️ Google Search Keys NOT found. Expecting fallback to DuckDuckGo (or failure if DDG is missing).")

    result = await perform_web_search(query)
    print("\n" + "="*50)
    print("SEARCH RESULT:")
    print("="*50)
    print(result)
    print("="*50)

if __name__ == "__main__":
    asyncio.run(test_search())
