# ⚡ Pɧơɛnıх (Phoenix)

![Phoenix in action](https://files.catbox.moe/qy3420.gif)

<p align="center">
  <a href="https://t.me/ThyRealPhoenixBot"><img src="https://img.shields.io/badge/Telegram-%40ThyRealPhoenixBot-2CA5E0?logo=telegram&logoColor=white" alt="Telegram Bot"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white" alt="Python 3.11+"></a>
  <a href="https://github.com/python-telegram-bot/python-telegram-bot"><img src="https://img.shields.io/badge/python--telegram--bot-11.1.0-blueviolet" alt="python-telegram-bot"></a>
  <a href="https://www.sqlalchemy.org/"><img src="https://img.shields.io/badge/SQLAlchemy-%3C2.0-e4473a" alt="SQLAlchemy"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/Gumballi/TheRealPhoenixBot" alt="License"></a>
</p>

<p align="center">
  <a href="https://github.com/Gumballi/TheRealPhoenixBot/stargazers"><img src="https://img.shields.io/github/stars/Gumballi/TheRealPhoenixBot?style=social" alt="GitHub Stars"></a>
  <a href="https://github.com/Gumballi/TheRealPhoenixBot/network"><img src="https://img.shields.io/github/forks/Gumballi/TheRealPhoenixBot?style=social" alt="GitHub Forks"></a>
  <a href="https://github.com/Gumballi/TheRealPhoenixBot/issues"><img src="https://img.shields.io/github/issues/Gumballi/TheRealPhoenixBot" alt="GitHub Issues"></a>
  <a href="https://github.com/Gumballi/TheRealPhoenixBot/pulls"><img src="https://img.shields.io/github/issues-pr/Gumballi/TheRealPhoenixBot" alt="Pull Requests"></a>
  <a href="https://github.com/Gumballi/TheRealPhoenixBot/commits/master"><img src="https://img.shields.io/github/last-commit/Gumballi/TheRealPhoenixBot" alt="Last Commit"></a>
  <a href="https://github.com/Gumballi/TheRealPhoenixBot"><img src="https://img.shields.io/github/repo-size/Gumballi/TheRealPhoenixBot" alt="Repo Size"></a>
  <a href="https://github.com/Gumballi/TheRealPhoenixBot"><img src="https://img.shields.io/badge/Maintained-yes-green.svg" alt="Maintained"></a>
</p>

A **modular Telegram group-management bot** running on Python 3 with a SQLAlchemy-powered PostgreSQL database.

Originally a **Marie fork**, Phoenix was created for personal use by [TheRealPhoenix](https://t.me/TheRealPhoenix) and is now maintained by [Gumball](https://t.me/Gumballization). Feel free to add it to your groups — it is available on Telegram as [@ThyRealPhoenixBot](https://t.me/ThyRealPhoenixBot).

---

## 📑 Table of Contents

- [Features](#-features)
- [Getting Started](#-getting-started)
- [Commands](#-commands)
- [Technology Stack](#-technology-stack)
- [Configuration](#-configuration)
- [Contributing](#-contributing)
- [Support](#-support)
- [FAQ](#-faq)
- [Credits](#-credits)
- [License](#-license)

---

## ✨ Features

Phoenix ships with a rich, plug-and-play module set for full group moderation and a few extras:

| Category | Modules |
| :--- | :--- |
| **Moderation** | Admin, Bans, Muting, Warns, Blacklist, Global Bans (Federations), Antiflood, Locks, Reporting, Message Deleting |
| **Chat Tools** | Welcome, Notes, Rules, Custom Filters, Disable Commands, AFK, Chatbot (AI), Sed, Secret |
| **Utilities** | Translator, Gemini AI, Urban Dictionary, Dictionary, Lyrics, Last.fm, MyAnimeList, Simkl, RSS, Wallpaper, Subtitles |
| **Power** | Eval, Shell, Web Server, Backups, Log Channel, Connection, Blacklist Chats, User Info |

---

## 🚀 Getting Started

Phoenix runs entirely on Telegram — no hosting or setup required on your end.

1. Open [@ThyRealPhoenixBot](https://t.me/ThyRealPhoenixBot) on Telegram and press **Start**.
2. Add the bot to your group.
3. Grant it admin permissions to unlock the full moderation suite.
4. Type `/help` to explore every available command.

### Deploy Your Own Instance

> ⚠️ **Note:** Heroku is no longer officially supported. If you can't get it working on Heroku, please don't come to the support chat and complain or ask for assistance.

[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/Gumballi/TheRealPhoenixBot)

To build your own bot from this source, you can follow the steps in the [tgbot guide](https://github.com/PaulSonOfLars/tgbot/blob/master/README.md).

---

## ⌨️ Commands

A quick cheat-sheet of the most useful commands. Type `/help` in any chat for the full, up-to-date list.

| Category | Commands |
| :--- | :--- |
| **Moderation** | `/ban`, `/unban`, `/kick`, `/mute`, `/unmute`, `/warn`, `/warns`, `/strongwarn`, `/gban`, `/ungban`, `/promote`, `/demote`, `/adminlist`, `/pin`, `/unpin` |
| **Chat Setup** | `/setwelcome`, `/welcome`, `/setgoodbye`, `/goodbye`, `/setrules`, `/rules`, `/setflood`, `/flood`, `/lock`, `/locks`, `/unlock` |
| **Notes & Filters** | `/save`, `/notes`, `/get`, `/clear`, `/filter`, `/filters`, `/stop`, `/del` |
| **Federations** | `/newfed`, `/joinfed`, `/chatfed`, `/fedinfo`, `/fban`, `/unfban`, `/fbanlist` |
| **Extras** | `/afk`, `/ud`, `/define`, `/wiki`, `/lyrics`, `/lastfm`, `/anime`, `/tv`, `/rss`, `/wall`, `/stickerid`, `/steal`, `/shout`, `/weebify`, `/hug`, `/kiss`, `/slap` |

---

## 🛠 Technology Stack

- **Language:** Python 3.11+
- **Framework:** [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) `11.1.0`
- **Database:** [SQLAlchemy](https://www.sqlalchemy.org/) `<2.0` with PostgreSQL (via `psycopg2`)
- **Extras:** Pillow, aiohttp, feedparser, google-genai, mistralai, and more

---

## ⚙️ Configuration

The bot can be configured via environment variables or by editing `tg_bot/config.py` from `sample_config.py`.

| Variable | Description |
| :--- | :--- |
| `TOKEN` | Your bot token from [@BotFather](https://t.me/BotFather) |
| `OWNER_ID` | Your Telegram user ID as an integer |
| `OWNER_USERNAME` | Your Telegram username |
| `SUDO_USERS` | Space-separated list of user IDs with sudo permissions |
| `SUPPORT_USERS` | Space-separated list of user IDs with support (gban) permissions |
| `WHITELIST_USERS` | Space-separated list of user IDs that cannot be banned |
| `DATABASE_URL` | PostgreSQL connection URI |
| `ENV` | Set to anything to enable environment variable configuration |
| `DEL_CMDS` | Set to `True` to delete commands used without permission |
| `ALLOW_EXCL` | Set to `True` to enable `!` as a command prefix alongside `/` |
| `STRICT_GBAN` | Enforce gbans across new and existing groups |
| `BL_CHATS` | Space-separated list of chat IDs to auto-leave |
| `WEBHOOK` / `URL` / `PORT` | Webhook-based hosting settings |

> **Security:** Never hardcode secrets into `config.py`. Prefer environment variables or a `.env` file.

---

## 🤝 Contributing

Contributions are welcome! To get started:

1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/amazing-feature`).
3. Commit your changes (`git commit -m 'Add some amazing feature'`).
4. Push to the branch (`git push origin feature/amazing-feature`).
5. Open a pull request.

For feature requests or bug reports, please open an [issue](https://github.com/Gumballi/TheRealPhoenixBot/issues).

---

## 💬 Support

- **Bot:** [@ThyRealPhoenixBot](https://t.me/ThyRealPhoenixBot) on Telegram
- **Maintainer/Owner:** [Gumball](https://t.me/Gumballization)
- **Bug reports & feature requests:** [GitHub Issues](https://github.com/Gumballi/TheRealPhoenixBot/issues)

---

## ❓ FAQ

**Is the bot free to use?**
Yes. Phoenix is open source under the GPL-3.0 license, and the public bot on Telegram is free to add to any group.

**Do I need to host anything myself?**
No. Just add [@ThyRealPhoenixBot](https://t.me/ThyRealPhoenixBot) to your group. Self-hosting is optional and only needed if you want your own instance.

**Does the bot store my group's data?**
Phoenix stores only the settings you configure — welcome messages, notes, filters, warnings, and so on — in a PostgreSQL database via SQLAlchemy.

**Is the bot safe for my group?**
Yes. It is a standard group-management bot that only acts on commands issued by admins, so you remain in full control at all times.

**How do I get support or report a bug?**
Message the bot on Telegram or open a [GitHub issue](https://github.com/Gumballi/TheRealPhoenixBot/issues).

---

## 📜 Credits

- [SkittBot](https://github.com/skittles9823/SkittBot) for the stickers module.
- [SaitamaRobot](https://github.com/AnimeKaizoku/SaitamaRobot) for the evaluator and more.
- **MrYacha**, **Ayra Hikari**, and **Mizukito Akito** for Federations.
- **1maverick1** for welcome mutes.

For the original repository with all commit history and authorship, see [here](https://github.com/rsktg/Phoenix.git) and [here](https://github.com/fushinori/TheRealPhoenixBot.git).

---

## ⚖️ Disclaimer

- This project is provided **as-is** for educational and personal-use purposes only.
- New features may be added and occasional bug fixes will be released over time.
- The maintainers are not responsible for any misuse of this bot.

---

## 📄 License

This project is licensed under the **GPL-3.0 License**. See the [LICENSE](LICENSE) file for details.

<p align="center">
  <sub>Developed with ❤️ by <a href="https://github.com/Gumballi">Gumballi</a></sub>
</p>
