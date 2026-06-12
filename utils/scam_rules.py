# utils/scam_rules.py
import re
from urllib.parse import urlparse

# Target domains for lookalike/phishing checks
OFFICIAL_DOMAINS = {
    "discord.com": ["discordapp.com", "discord.gg", "discord.media", "discordapp.net"],
    "steamcommunity.com": ["steampowered.com", "steamgames.com", "valvesoftware.com"],
    "roblox.com": ["roblox.education", "rbxcdn.com"],
    "epicgames.com": ["unrealengine.com", "fortnite.com"]
}

# Flattens all valid domains into a lookup set
ALL_VALID_DOMAINS = {dom for key, sublist in OFFICIAL_DOMAINS.items() for dom in [key] + sublist}

# Levenshtein distance helper
def levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]

def extract_urls(text: str) -> list[str]:
    """Extracts all HTTP/HTTPS links and raw www. domains from text, cleaning trailing punctuation."""
    pattern = r'(https?://[^\s<>"]+|www\.[^\s<>"]+)'
    raw_urls = re.findall(pattern, text, re.IGNORECASE)
    urls = []
    for url in raw_urls:
        # Strip trailing punctuation marks commonly found in text/sentences
        url = url.rstrip('.,!?)-_*"\'')
        if url not in urls:
            urls.append(url)
            
    # Also capture raw discord invites
    invites = re.findall(r'(discord\.gg/[^\s<>"]+)', text, re.IGNORECASE)
    for inv in invites:
        clean_inv = inv.rstrip('.,!?)-_*"\'')
        full_inv = "https://" + clean_inv
        if full_inv not in urls:
            urls.append(full_inv)
    return urls

def normalize_domain(url: str) -> str:
    """Extracts domain from url and removes subdomains (like www., gift., nitro.)."""
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        # Handle port
        if ":" in netloc:
            netloc = netloc.split(":")[0]
        # Remove common prefix subdomains
        parts = netloc.split('.')
        if len(parts) > 2:
            # Keep only the last two parts unless it's a co.uk etc.
            if parts[-2] in ('co', 'com', 'org', 'net', 'gov', 'edu', 'ac') and len(parts) > 3:
                return '.'.join(parts[-3:])
            return '.'.join(parts[-2:])
        return netloc
    except Exception:
        return ""

def is_lookalike_domain(domain: str) -> tuple[bool, str]:
    """
    Checks if a domain is a malicious typosquatting lookalike of an official domain.
    E.g. discorcl.com, steamcommunnity.com, discord-gift.ru.
    """
    if not domain or domain in ALL_VALID_DOMAINS:
        return False, ""

    # Rule 1: Substring phishing checks (e.g. "discord-nitro.ru" contains discord but isn't official)
    for official in OFFICIAL_DOMAINS.keys():
        name_only = official.split('.')[0] # "discord", "steamcommunity", etc.
        if name_only in domain:
            return True, f"Suspicious substring: '{name_only}' found in unofficial domain '{domain}'"

    # Rule 2: Edit distance checks for typosquatting (Levenshtein distance <= 2)
    for official in ALL_VALID_DOMAINS:
        off_name = official.split('.')[0]
        dom_name = domain.split('.')[0]
        
        # We only compare similar length names to avoid false matches
        if abs(len(dom_name) - len(off_name)) <= 3:
            dist = levenshtein_distance(dom_name, off_name)
            # If distance is small, it's highly likely to be a lookalike
            if 0 < dist <= 2:
                return True, f"Lookalike domain detected: '{domain}' (Levenshtein distance {dist} from '{official}')"

    return False, ""

def matches_scam_signatures(text: str, urls: list[str]) -> tuple[bool, str]:
    """
    Scans text for specific combinations of phrases that indicate Discord phishing/scam attempts.
    This is highly accurate when combined with a URL check.
    """
    text_lower = text.lower()
    has_url = len(urls) > 0

    # Strong single indicator signatures (don't even need a URL sometimes, but we require it to be safe)
    if has_url:
        # Combo 1: MrBeast giveaways
        if "mrbeast" in text_lower and any(x in text_lower for x in ("giveaway", "crypto", "casino", "free", "bonus")):
            return True, "Signature match: Fake MrBeast promotion combined with a link."

        # Combo 2: Free Nitro phishing
        if "nitro" in text_lower and any(x in text_lower for x in ("free", "gift", "airdrop", "claimed", "promo", "generator")):
            return True, "Signature match: Nitro phishing signature combined with a link."

        # Combo 3: Free Crypto / Casino bonuses
        if "casino" in text_lower and any(x in text_lower for x in ("bonus", "deposit", "withdraw", "free", "cryptocurrency")):
            return True, "Signature match: Crypto casino promo signature combined with a link."

        # Combo 4: General phishing urgencies
        urgencies = ("claim", "activate", "claim immediately", "limited time", "don't miss")
        scam_words = ("bonus", "free money", "voucher", "gift card", "distribution")
        if any(u in text_lower for u in urgencies) and any(sw in text_lower for sw in scam_words):
            return True, "Signature match: High urgency scam call-to-action combined with a link."

        # Combo 5: Steam trading scams
        if "steam" in text_lower and any(x in text_lower for x in ("trade", "gift", "roll", "skin", "csgo")):
            return True, "Signature match: Steam inventory/trade scam signature combined with a link."

    return False, ""
