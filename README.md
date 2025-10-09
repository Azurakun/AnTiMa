# AnTiMa - The Interactive AI Discord Bot

<p align="center">
  <img src="https://img.shields.io/badge/Discord.py-2.5.2-7289DA?style=for-the-badge&logo=discord&logoColor=white" alt="Discord.py">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Google-Gemini_AI-4285F4?style=for-the-badge&logo=google&logoColor=white" alt="Gemini AI">
</p>

**AnTiMa** is not just another Discord bot; it's a fully interactive companion for your server, powered by Google's Gemini AI. With a customizable personality, real-time voice conversations, and a suite of powerful moderation and utility tools, AnTiMa is designed to bring your community to life.

-----

## ✨ Core Features

AnTiMa is organized into several modules, each providing a distinct set of features.

### 🎙️ Real-Time Voice Conversations

Bring the AI directly into your voice channels\! AnTiMa can join, listen, and respond in real-time using speech-to-text and text-to-speech.

  - **`/joinchat`**: Asks the bot to join your current voice channel and start a conversation.
  - **`/leavechat`**: The bot will leave the voice channel and end the conversation.

### 💬 Live AI Chat Interaction

AnTiMa can actively participate in text channels, responding to mentions or chatting freely in a designated channel. The bot's personality is fully customizable, allowing you to create a unique experience for your server.

  - **Memory**: The bot remembers past conversations with users to provide contextually aware responses. This allows for more natural and engaging interactions.
  - **Customizable Personality**: Easily swap out the system prompt to change the bot's entire personality—from a sarcastic teen to a helpful friend.
  - **Proactive Chat**: AnTiMa can initiate conversations with users in the designated chat channel, helping to keep the community active.
  - **Adaptive Personality**: The bot can analyze recent conversations in a server and generate a "Style Guide" to adapt its personality to the server's vibe.
  - **`/setchatchannel`**: Designate a specific channel where the bot will reply to all messages.
  - **`/setchatforum`**: Set a forum where the bot will engage with posts.
  - **`/refreshpersonality`**: Manually trigger the adaptive personality update for the server.
  - **`/clearmemories`**: Allows users to clear their personal conversation history with the bot. Admins can clear memories for other users or the entire guild.
  - **`/togglegroupchat`**: Enables or disables grouped responses in the chat channel.
  - **`/startchat`**: Manually initiates a proactive conversation with a user.

### 🎨 Anime Image Search

  - **`/animeimage [tags]`**: Fetches a random high-quality anime image from the Danbooru API, with tag autocomplete and an interactive "Another One\!" button.

### ⏰ Reminders

  - **`/remindme [when] [message] [repeat]`**: Sets a personal reminder. You can use simple time formats like `10m`, `2h30m`, or a specific time like `16:30`.
  - **`/settimezone [timezone]`**: Sets your local timezone for accurate reminders.

### ⚙️ Automated Role Management

  - **`/setjoinrole [role]`**: Automatically assign a specific role to every new member upon joining.
  - **`/clearjoinrole`**: Disables the automatic role assignment for new members.
  - **`/setemojirole [message_id] [emoji] [role]`**: Set up a powerful reaction role system on any message.

### 👑 Administrator Tools

  - **`/msg [channel_id] [message]`**: Send a message as the bot to a specified channel. Supports mentions and custom embeds with titles and colors.
  - **`/purgelogs`**: Deletes bot-related log data from the database for privacy and maintenance.

-----

## 🚀 Getting Started

Follow these instructions to get a copy of the bot up and running.

### Prerequisites

  - Python 3.11 or higher
  - A Discord Bot Token from the [Discord Developer Portal](https://discord.com/developers/applications)
  - A **Google Gemini API Key** from [Google AI Studio](https://aistudio.google.com/app/apikey)
  - An **ElevenLabs API Key** for voice interactions.
  - A **MongoDB URI** for database storage
  - A **Tenor API Key** for GIF searches.

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

      - Create a file named `.env` in the root directory.
      - Add your credentials to this file:
        ```env
        TOKEN="YOUR_DISCORD_BOT_TOKEN_HERE"
        GEMINI_API_KEY="YOUR_GEMINI_API_KEY_HERE"
        ELEVENLABS_API_KEY="YOUR_ELEVENLABS_API_KEY_HERE"
        MONGO_URL="YOUR_MONGODB_CONNECTION_STRING_HERE"
        TENOR_API_KEY="YOUR_TENOR_API_KEY_HERE"
        ```

4.  **Run the bot:**

    ```bash
    python main.py
    ```

    Your bot should now be online and ready to use\!

-----

## 📝 How to Use

All commands are available as slash commands. Simply type `/` in a server where the bot is present to see a list of all available commands.

### Command Examples

  - **Have a voice conversation with the bot:**

    ```
    /joinchat
    ```

  - **Get a random image of a specific character:**

    ```
    /animeimage tags:bocchi
    ```

  - **Set a welcome role for new members:**

    ```
    /setjoinrole role:@Member
    ```

  - **Set a reminder:**

    ```
    /remindme when:2h30m message:Check on the bot
    ```

## 🤝 Contributing

Contributions, issues, and feature requests are welcome\! Feel free to check the [issues page](https://github.com/azurakun/AnTiMa/issues).