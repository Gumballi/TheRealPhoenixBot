import random
import re

import requests
from telegram import Update, Bot
from telegram.ext import MessageHandler, Filters, run_async

from tg_bot import dispatcher, TOKEN, LOGGER

__mod_name__ = "Reactions"

__help__ = """
The bot reacts to greetings in chat:
 - Good morning messages (good morning, gm, gmorning, gmorn, gmornin) get a random heart reaction.
 - Good night messages (good night, goodnight, gn, gnite, gnight) get a random heart reaction.

This module has no commands. Only whole words or phrases are matched.
"""

REACTION_EMOJIS = ["❤️", "🥰", "🐳"]

MORNING_RE = re.compile(r"\b(good\s*morning|gmornin(g)?|gm)\b", re.IGNORECASE)
NIGHT_RE = re.compile(r"\b(good\s*night|goodnight|gnite|gnight|gn)\b", re.IGNORECASE)

SET_REACTION_URL = "https://api.telegram.org/bot{}/setMessageReaction"

LOGGER.info("[REACTIONS_DBG] Reactions module file imported (module level)")


def _matches_greeting(text: str) -> bool:
    return bool(MORNING_RE.search(text) or NIGHT_RE.search(text))


@run_async
def greeting_reaction(bot: Bot, update: Update):
    try:
        LOGGER.info("[REACTIONS_DBG] callback entered")
        message = update.effective_message
        chat = update.effective_chat
        LOGGER.info(
            "[REACTIONS_DBG] chat_id=%s has_message=%s text=%r",
            chat.id if chat else None,
            bool(message),
            message.text if message else None,
        )
        if not message or not chat or not message.text:
            LOGGER.info("[REACTIONS_DBG] skipped: missing message, chat, or text")
            return

        if not _matches_greeting(message.text):
            LOGGER.info("[REACTIONS_DBG] skipped: text did not match greeting regex")
            return

        chosen_emoji = random.choice(REACTION_EMOJIS)
        LOGGER.info("[REACTIONS_DBG] matched; sending reaction %s to message %s", chosen_emoji, message.message_id)
        response = requests.post(
            SET_REACTION_URL.format(TOKEN),
            json={
                "chat_id": chat.id,
                "message_id": message.message_id,
                "reaction": [{"type": "emoji", "emoji": chosen_emoji}],
            },
            timeout=5,
        )
        LOGGER.info("[REACTIONS_DBG] setMessageReaction HTTP %s: %s", response.status_code, response.text)
        if not response.ok:
            LOGGER.warning(
                "setMessageReaction failed in chat %s (HTTP %s): %s",
                chat.id, response.status_code, response.text,
            )
    except requests.RequestException as e:
        LOGGER.error("setMessageReaction request error: %s", e)
    except Exception:
        LOGGER.exception("[REACTIONS_DBG] unexpected error in greeting_reaction")


# Group 30 keeps this handler from blocking or being blocked by the default
# group 0 handlers, and it does not collide with groups used by other modules.
REACTION_HANDLER = MessageHandler(Filters.text, greeting_reaction)

dispatcher.add_handler(REACTION_HANDLER, group=30)
LOGGER.info("[REACTIONS_DBG] handler registered on group 30")
