# Maintenance scripts

One-off debugging and maintenance tools. They are not part of the bot.
Run them from the repository root as modules so project imports resolve:

```bash
python -m scripts.check_search          # try the DuckDuckGo web search helper
python -m scripts.check_google_search   # try the Google Custom Search API keys
python -m scripts.check_groq_models     # list models available to GROQ_API_KEY
python -m scripts.debug_danbooru        # fetch a random gacha image
python -m scripts.debug_safebooru
python -m scripts.restore_game          # restore an RPG session (read the script first)
```
