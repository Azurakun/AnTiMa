
import asyncio
import aiohttp
import json

async def test_safebooru():
    url = "https://safebooru.org/index.php"
    params = {
        "page": "dapi",
        "s": "post",
        "q": "index",
        "json": "1",
        "limit": "1",
        "tags": "rating:safe"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            print(f"Status: {resp.status}")
            if resp.status == 200:
                data = await resp.json() # content_type=None might be needed if they send text/html
                print("Data Sample:", json.dumps(data[0], indent=2))
            else:
                print(await resp.text())

async def test_autocomplete():
    url = "https://safebooru.org/index.php"
    params = {
        "page": "dapi",
        "s": "tag",
        "q": "index",
        "json": "1",
        "name_pattern": "genshin%",
        "limit": "5"
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            print(f"\nAutocomplete Status: {resp.status}")
            if resp.status == 200:
                data = await resp.json()
                print("Autocomplete Sample:", json.dumps(data[0], indent=2))

if __name__ == "__main__":
    asyncio.run(test_safebooru())
    asyncio.run(test_autocomplete())
