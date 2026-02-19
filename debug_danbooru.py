
import asyncio
import logging
from utils.danbooru_api import get_random_danbooru_image

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("utils.danbooru_api")
logger.setLevel(logging.DEBUG)

async def main():
    print("--- Starting Danbooru Debug ---")
    try:
        # Test without tag
        print("\nTest 1: Random Image (No tag)")
        result = await get_random_danbooru_image()
        print(f"Result 1: {result}")
        
        # Test with tag
        print("\nTest 2: Specific Tag (genshin_impact)")
        result2 = await get_random_danbooru_image("genshin_impact")
        print(f"Result 2: {result2}")

        # Test Autocomplete
        print("\nTest 3: Autocomplete (gen)")
        from utils.danbooru_api import danbooru_tag_autocomplete
        result3 = await danbooru_tag_autocomplete("gen")
        print(f"Result 3: {result3}")

    except Exception as e:
        print(f"CRITICAL FAULT: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
