# utils/db.py
import pymongo
import os

# Get the MongoDB connection string from the environment variables
MONGO_URL = os.environ.get("MONGO_URL")

# Establish a connection to the MongoDB server
client = pymongo.MongoClient(MONGO_URL)

# Select the database you want to use
db = client.get_database("antima_db") # You can name your database whatever you want

# Create collections for your cogs (similar to tables in SQL)
reminders_collection = db.get_collection("reminders")
user_timezones_collection = db.get_collection("user_timezones")
ai_config_collection = db.get_collection("ai_config")
ai_memories_collection = db.get_collection("ai_memories") # <-- ADD THIS LINE