# Discord AI & Moderation Bot

<div align="center">
  <img src="https://img.shields.io/badge/Discord.py-2.3.2-7289DA?style=for-the-badge&logo=discord&logoColor=white" alt="Discord.py">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Status-Online-green?style=for-the-badge&logo=discord" alt="Bot Status">
</div>

A powerful, modular Discord bot built with `discord.py` that provides features for anime image searching, automated role management, and server administration.

## ✨ Features

This bot comes packed with features to enhance any Discord server:

---

### 🎨 Anime Image Search
-   `/animeimage [tags]`: Fetches a random high-quality anime image from the Danboor API, with tag autocomplete and an interactive "Another One!" button.

![Anime Command Demo](assets/anime-demo.gif)

---

### ⚙️ Automated Role Management
-   `/setjoinrole [role]`: Automatically assign a specific role to every new member upon joining.
-   `/setemojirole [message_id] [emoji] [role]`: Set up a powerful reaction role system on any message.

![Join Role Demo](assets/joinrole-demo.gif)

---

### 👑 Administrator Tools
-   `/msg [channel_id] [message]`: Send a message as the bot to a specified channel. Supports mentions and custom embeds with titles and colors.

![Message Command Demo](assets/msg-demo.gif)

---

## 🚀 Getting Started

Follow these instructions to get a copy of the bot up and running on your local machine or server.

### Prerequisites

-   Python 3.8 or higher
-   A Discord Bot Token from the [Discord Developer Portal](https://discord.com/developers/applications)

### Installation & Setup

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/your-username/your-repo-name.git](https://github.com/your-username/your-repo-name.git)
    cd your-repo-name
    ```

2.  **Install the required libraries:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Configure your bot token:**
    -   Create a file named `.env` in the root directory of the project.
    -   Add your bot token to this file:
        ```
        TOKEN="YOUR_DISCORD_BOT_TOKEN_HERE"
        ```

4.  **Run the bot:**
    ```bash
    python main.py
    ```
    Your bot should now be online and ready to use!

## 📝 How to Use

All commands are available as slash commands. Simply type `/` in a server where the bot is present to see a list of all available commands.

### Command Examples

-   **Get a random image of a specific character:**
    ```
    /animeimage tags:bocchi
    ```

-   **Set a welcome role for new members:**
    ```
    /setjoinrole role:@Member
    ```

## 🤝 Contributing

Contributions, issues, and feature requests are welcome! Feel free to check the [issues page](https://github.com/your-username/your-repo-name/issues).
