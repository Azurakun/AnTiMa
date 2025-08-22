# AnTiMa - The Interactive AI Discord Bot

<div align="center">
  <img src="https://img.shields.io/badge/Discord.py-2.5.2-7289DA?style=for-the-badge&logo=discord&logoColor=white" alt="Discord.py">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/Google-Gemini_AI-4285F4?style=for-the-badge&logo=google&logoColor=white" alt="Gemini AI">

</div>

**AnTiMa** is not just another Discord bot; it's a fully interactive companion for your server, powered by Google's Gemini AI. With a customizable personality, real-time voice conversations, and a suite of powerful moderation tools, AnTiMa is designed to bring your community to life.

## ✨ Core Features

---

### 🎙️ Real-Time Voice Conversations
Bring the AI directly into your voice channels! AnTiMa can join, listen, and respond in real-time.

-   **/joinchat**: Asks the bot to join your current voice channel and start a conversation.
-   **/leavechat**: The bot will leave the voice channel.

![Voice Chat Demo](assets/voice-demo.gif) 

---

### 💬 Live AI Chat Interaction
AnTiMa can actively participate in text channels, responding to mentions or chatting freely in a designated channel. The bot's personality is fully customizable, allowing you to create a unique experience for your server.

-   **Memory**: The bot remembers past conversations with users to provide contextually aware responses.
-   **Customizable Personality**: Easily swap out the system prompt to change the bot's entire personality—from a sarcastic teen to a helpful friend.
-   **/setchatchannel**: Designate a specific channel where the bot will reply to all messages.
-   **/setchatforum**: Set a forum where the bot will engage with posts.

![AI Chat Demo](assets/chat-demo.gif)

---

### 🎨 Anime Image Search
-   `/animeimage [tags]`: Fetches a random high-quality anime image from the Danbooru API, with tag autocomplete and an interactive "Another One!" button.

![Anime Command Demo](assets/anime-demo.gif)

---

### ⚙️ Automated Role Management
-   `/setjoinrole [role]`: Automatically assign a specific role to every new member upon joining.
-   `/setemojirole [message_id] [emoji] [role]`: Set up a powerful reaction role system on any message.

![Join Role Demo](assets/joinrole-demo.gif)

---

### 👑 Administrator Tools
-   `/msg [channel_id] [message]`: Send a message as the bot to a specified channel. Supports mentions and custom embeds with titles and colors.
-   `/purgelogs`: Deletes bot-related log data from the database for privacy and maintenance.

![Message Command Demo](assets/msg-demo.gif)

---

## 🚀 Getting Started

Follow these instructions to get a copy of the bot up and running.

### Prerequisites

-   Python 3.11 or higher
-   A Discord Bot Token from the [Discord Developer Portal](https://discord.com/developers/applications)
-   A **Google Gemini API Key** from [Google AI Studio](https://aistudio.google.com/app/apikey)
-   A **MongoDB URI** for database storage

### Installation & Setup

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/azurakun/AnTiMa.git](https://github.com/azurakun/AnTiMa.git)
    cd AnTiMa
    ```

2.  **Install the required libraries:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Configure your environment variables:**
    -   Create a file named `.env` in the root directory.
    -   Add your credentials to this file:
        ```env
        TOKEN="YOUR_DISCORD_BOT_TOKEN_HERE"
        GEMINI_API_KEY="YOUR_GEMINI_API_KEY_HERE"
        MONGO_URL="YOUR_MONGODB_CONNECTION_STRING_HERE"
        ```

4.  **Run the bot:**
    ```bash
    python main.py
    ```
    Your bot should now be online and ready to use!

## 📝 How to Use

All commands are available as slash commands. Simply type `/` in a server where the bot is present to see a list of all available commands.

### Command Examples

-   **Have a voice conversation with the bot:**
    ```
    /joinchat
    ```

-   **Get a random image of a specific character:**
    ```
    /animeimage tags:bocchi
    ```

-   **Set a welcome role for new members:**
    ```
    /setjoinrole role:@Member
    ```

## 🤝 Contributing

Contributions, issues, and feature requests are welcome! Feel free to check the [issues page](https://github.com/azurakun/AnTiMa/issues).