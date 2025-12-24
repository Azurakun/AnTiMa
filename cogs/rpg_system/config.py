# cogs/rpg_system/config.py

RPG_CLASSES = {
    "Warrior": {"hp": 120, "mp": 20, "stats": {"STR": 16, "DEX": 10, "INT": 8, "CHA": 10}, "skills": ["Greatslash", "Taunt"]},
    "Mage": {"hp": 60, "mp": 100, "stats": {"STR": 6, "DEX": 12, "INT": 18, "CHA": 10}, "skills": ["Fireball", "Teleport"]},
    "Rogue": {"hp": 80, "mp": 50, "stats": {"STR": 10, "DEX": 18, "INT": 12, "CHA": 14}, "skills": ["Backstab", "Stealth"]},
    "Cleric": {"hp": 90, "mp": 80, "stats": {"STR": 12, "DEX": 8, "INT": 14, "CHA": 16}, "skills": ["Heal", "Smite"]},
    "Freelancer": {"hp": 100, "mp": 50, "stats": {"STR": 12, "DEX": 12, "INT": 12, "CHA": 10}, "skills": ["Improvise", "Run"]}
}

PREMADE_CHARACTERS = [
    {
        "name": "Kaelen the Driftwood",
        "class": "Warrior",
        "age": 34,
        "pronouns": "He/Him",
        "appearance": "Rugged, scarred face, wears worn leather armor and carries a chipped greatsword.",
        "personality": "Stoic, protective, but secretly loves cute animals.",
        "hobbies": "Woodcarving, sharpening swords.",
        "backstory": "A former soldier who deserted a corrupt army. Now wanders seeking redemption.",
        "alignment": "Neutral Good",
        "stats": {"STR": 16, "DEX": 12, "INT": 10, "CHA": 8}
    },
    {
        "name": "Elara Moonwhisper",
        "class": "Mage",
        "age": 22,
        "pronouns": "She/Her",
        "appearance": "Silver hair, glowing violet eyes, robes embroidered with constellations.",
        "personality": "Curious, socially awkward, obsessed with ancient history.",
        "hobbies": "Stargazing, reading dusty tomes.",
        "backstory": "Expelled from the Academy for 'accidental explosive decompression'.",
        "alignment": "Chaotic Good",
        "stats": {"STR": 8, "DEX": 10, "INT": 18, "CHA": 12}
    },
    {
        "name": "Lyna Shadowstep",
        "class": "Rogue",
        "age": 19,
        "pronouns": "They/Them",
        "appearance": "Hooded figure, masked, bandaged hands, multiple knives visible.",
        "personality": "Sarcastic, greedy, but loyal to friends.",
        "hobbies": "Gambling, lockpicking.",
        "backstory": "Grew up in the slums. Learned that coin is the only god worth praying to.",
        "alignment": "Chaotic Neutral",
        "stats": {"STR": 10, "DEX": 18, "INT": 14, "CHA": 10}
    }
]

SCENARIOS = [
    {
        "label": "The Cyber-Dungeon", 
        "value": "Cyberpunk Fantasy", 
        "genre": "[Sci-Fi / Magic]",
        "desc": "The year is 2099. Neo-Veridia is a sprawling metropolis of neon lights and chrome, ruled by the iron fist of the Mega-Corporations. But beneath the shiny surface, ancient magic has reawakened."
    },
    {
        "label": "The Haunted High School", 
        "value": "High School Horror", 
        "genre": "[Horror / Mystery]",
        "desc": "Kisaragi Academy was closed fifty years ago after the 'incident'. Now, it has reopened, but the stains of the past remain. Students whisper of the Seven Mysteries."
    },
    {
        "label": "Isekai: The Trash Hero", 
        "value": "Generic Isekai", 
        "genre": "[Comedy / Adventure]",
        "desc": "Truck-kun hit you. You wake up in a generic fantasy world with a useless skill and a Demon Lord to defeat. Time to grind."
    },
    {
        "label": "Eldritch Noir", 
        "value": "Lovecraftian Detective", 
        "genre": "[Mystery / Horror]",
        "desc": "1920s Providence. The rain never stops. Cultists whisper of sleeping gods. You are a PI with a dwindling supply of sanity."
    }
]