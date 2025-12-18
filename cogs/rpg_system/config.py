# cogs/rpg_system/config.py

RPG_CLASSES = {
    "Warrior": {"hp": 120, "mp": 20, "stats": {"STR": 16, "DEX": 10, "INT": 8, "CHA": 10}, "skills": ["Greatslash", "Taunt"]},
    "Mage": {"hp": 60, "mp": 100, "stats": {"STR": 6, "DEX": 12, "INT": 18, "CHA": 10}, "skills": ["Fireball", "Teleport"]},
    "Rogue": {"hp": 80, "mp": 50, "stats": {"STR": 10, "DEX": 18, "INT": 12, "CHA": 14}, "skills": ["Backstab", "Stealth"]},
    "Cleric": {"hp": 90, "mp": 80, "stats": {"STR": 12, "DEX": 8, "INT": 14, "CHA": 16}, "skills": ["Heal", "Smite"]},
    "Student": {"hp": 100, "mp": 100, "stats": {"STR": 10, "DEX": 10, "INT": 12, "CHA": 16}, "skills": ["Persuade", "Study", "Drama"]}, 
    "Detective": {"hp": 90, "mp": 70, "stats": {"STR": 10, "DEX": 12, "INT": 16, "CHA": 12}, "skills": ["Investigate", "Deduce", "Shoot"]},
    "Freelancer": {"hp": 100, "mp": 50, "stats": {"STR": 12, "DEX": 12, "INT": 12, "CHA": 10}, "skills": ["Improvise", "Run"]}
}

SCENARIOS = [
    {"label": "The Cyber-Dungeon", "value": "Cyberpunk Fantasy", "desc": "Hackers & Dragons in a neon city.", "genre": "[Sci-Fi]"},
    {"label": "The Haunted High School", "value": "High School Horror", "desc": "Survive the ghosts of the old building.", "genre": "[Horror]"},
    {"label": "Isekai Trash", "value": "Generic Isekai", "desc": "Hit by a truck, now you're a hero.", "genre": "[Comedy]"},
    {"label": "Sakura Academy (Male POV)", "value": "Slice of Life", "desc": "You are the only male student in an elite all-girls school.", "genre": "[Romance]"},
    {"label": "Sakura Academy (Female POV)", "value": "Slice of Life", "desc": "You are a female student in an elite all-girls school.", "genre": "[Slice of Life]"}
]