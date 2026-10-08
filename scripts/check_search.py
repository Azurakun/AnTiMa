import asyncio
from cogs.ai_chat.utils import perform_web_search

async def main():
    print("Testing search...")
    result = await perform_web_search("current time in Tokyo")
    print(result)

if __name__ == "__main__":
    asyncio.run(main())
