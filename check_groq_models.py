import requests
from dotenv import load_dotenv
import os

load_dotenv()
api_key = os.environ.get("GROQ_API_KEY")

if not api_key or "YOUR_" in api_key:
    print(f"ERROR: GROQ_API_KEY not set or is still placeholder")
    print(f"Current value: {api_key}")
    exit()

if api_key.startswith("xai-"):
    print("ERROR: This is an xAI key (starts with xai-), not a Groq key")
    print("Get a Groq key from: https://console.groq.com/keys")
    print("Groq keys start with 'gsk_'")
    exit()

print(f"Using key: {api_key[:8]}...{api_key[-4:]}")
print()

r = requests.get("https://api.groq.com/openai/v1/models", headers={
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
})

if r.status_code != 200:
    print(f"API Error {r.status_code}: {r.text}")
    exit()

models = sorted(r.json().get("data", []), key=lambda x: x["id"])
print(f"Available models ({len(models)}):")
print("-" * 60)
for m in models:
    owned = m.get("owned_by", "?")
    print(f"  {m['id']:<45} ({owned})")
