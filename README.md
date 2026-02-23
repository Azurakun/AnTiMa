# AnTiMa - The Interactive AI Discord Bot

<p align="center">
  <img src="https://img.shields.io/badge/Discord.py-3.0-7289DA?style=for-the-badge&logo=discord&logoColor=white" alt="Discord.py">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Google-Gemini_AI-4285F4?style=for-the-badge&logo=google&logoColor=white" alt="Gemini AI">
</p>

**AnTiMa** is not just another Discord bot; it's a fully interactive companion for your server, powered by Google's Gemini AI. With a customizable personality, a deep and engaging RPG system, and a suite of powerful moderation and utility tools, AnTiMa is designed to bring your community to life. It also features a web-based dashboard for easy management.

---

## ✨ Core Features

AnTiMa is organized into several modules, known as "cogs", each providing a distinct set of features.

### 🧠 AI Chat & Personality

The core of AnTiMa is its advanced AI chat functionality, designed for natural and engaging interaction.

-   **Contextual Memory**: The bot remembers past conversations with users to provide contextually aware responses.
-   **Adaptive Personality**: AnTiMa can analyze recent server conversations to learn the "vibe" and adapt its speaking style accordingly.
-   **Proactive Chat**: Can be configured to initiate conversations periodically, keeping the community active.
-   **Group Chat Awareness**: Can participate in general conversation in a designated channel, not just respond to mentions.
-   **Slash Commands**:
    -   `/ai forget`: Clears the bot's memory of your conversations.
    -   `/ai lore`: Shows what the bot has learned about your server.
    -   `/ai teach`: (Admin) Manually provide the bot with information about your server.
    -   `/ai refresh`: (Admin) Force the bot to re-analyze the server's personality.

### ⚔️ Immersive RPG System

Dive into limitless adventures with a persistent, AI-driven role-playing game.

-   **Dynamic World**: The AI Game Master creates and manages a world with evolving quests, NPCs, and locations.
-   **Web Dashboard**: A full-featured web interface to create new adventures, manage characters, and inspect the game state.
-   **Character Personas**: Create and save your own characters to use in different adventures.
-   **Persistent State**: The bot remembers everything that happens in the game world, from player actions to NPC relationships.
-   **Dynamic Action UI**: The AI generates context-aware action buttons (e.g., "Examine the mysterious altar" or "Try to persuade the guard"), providing an intuitive way to play. This can be toggled off for a classic text-based experience.
-   **Slash Commands**:
    -   `/rpg start`: Start a new RPG adventure.
    -   `/rpg world`: Inspect the current state of the game world.
    -   `/rpg rewind`: (GM) Rewind the story to a previous turn.
    -   `/rpg sync`: Re-synchronize the game state from the chat history.
    -   `/rpg web_new`: Create a new adventure using the web dashboard.
    -   `/rpg personas`: Manage your saved character personas.
    -   `/rpg uimode`: (GM) Switch between button and text-based UI.
    -   `/rpg end`: End the current adventure.

### 💮 Anime Gacha Game

A fun "gacha" mini-game for collecting anime-style character cards.

-   **Pull for Characters**: Use credits to "pull" for random waifus and husbandos.
-   **Rarity System**: Characters have different rarities based on their popularity.
-   **Daily Credits**: Claim daily credits to fund your pulls.
-   **Slash Commands**:
    -   `/daily`: Claim your daily credits.
    -   `/waifu` / `/husbando`: Pull for a random character.
    -   `/profile`: View your gacha game profile.
    -   `/inventory`: See the characters you've collected.

### 🛠️ Server Utilities

A collection of tools to manage and enhance your server.

-   **Automated Welcome Role**: Automatically assign a role to new members.
-   **Reaction Roles**: Set up roles that users can get by reacting to a message.
-   **Reminders**: Set personal reminders for yourself.
-   **Timezone Support**: Set your timezone for accurate reminder times.
-   **Slash Commands**:
    -   `/setjoinrole` & `/clearjoinrole`: (Admin) Manage the auto-role for new members.
    -   `/remindme` & `/settimezone`: Manage personal reminders.

### 🛡️ Moderation

-   **Basic Moderation**: Kick and ban members, and purge messages.
-   **Slash Commands**:
    -   `/mod kick` / `/mod ban`: (Mod) Kick or ban a user.
    -   `/mod purge`: (Mod) Delete a number of messages.

### 🌐 Web Dashboard

A powerful web-based interface for managing the bot and viewing stats.

-   **Live Stats**: See live statistics for messages, commands, and server activity.
-   **RPG Management**: Create and manage RPG sessions, characters, and worlds.
-   **Configuration**: Configure bot settings directly from the web.
-   **Log Viewer**: View live bot logs for debugging.

---

## 🚀 Getting Started

Follow these instructions to get a copy of the bot up and running on your own machine.

### Prerequisites

-   Python 3.11 or higher
-   A Discord Bot Token from the [Discord Developer Portal](https://discord.com/developers/applications)
-   A **Google Gemini API Key** from [Google AI Studio](https://aistudio.google.com/app/apikey)
-   A **MongoDB URI** for database storage. You can get one for free from [MongoDB Atlas](https://www.mongodb.com/cloud/atlas/register).

### Installation & Setup

1.  **Clone the repository:**

    ```bash
    git clone https://github.com/azurakun/AnTiMa.git
    cd AnTiMa
    ```

2.  **Install the required libraries:**

    ```bash
    pip install -r requirements.txt
    ```

3.  **Configure your environment variables:**

    -   Create a file named `.env` in the root directory.
    -   Add your credentials to this file. You can use the `.env.example` file as a template.

    ```env
    DISCORD_TOKEN="YOUR_DISCORD_BOT_TOKEN_HERE"
    GEMINI_API_KEY="YOUR_GEMINI_API_KEY_HERE"
    MONGO_URL="YOUR_MONGODB_CONNECTION_STRING_HERE"
    ```

4.  **Run the bot:**

    ```bash
    python main.py
    ```

    Your bot should now be online, and the web dashboard should be accessible at `http://localhost:8000`.

---

## ⚙️ Basic Configuration

Once the bot is in your server, you can configure it using the `/config` commands.

1.  **Set the AI Chat Channel**:
    -   Use `/config chat` to choose the channel where the bot will be active and set how often it speaks.

2.  **Set the RPG Channel**:
    -   Use `/config rpg` to designate a channel where users can start new RPG adventures.

3.  **Enable/Disable the Bot**:
    -   Use `/config bot` to turn the bot's AI chat features on or off for the entire server.

Now you're all set to explore everything AnTiMa has to offer!
