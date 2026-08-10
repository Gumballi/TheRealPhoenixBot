import html
import logging
from time import sleep
from typing import List

from telegram import Update, Bot, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import CommandHandler, Filters
from telegram.ext.dispatcher import run_async
from telegram.utils.helpers import mention_html

from tg_bot import dispatcher
from tg_bot.modules.helper_funcs.chat_status import is_bot_admin, is_user_admin
from tg_bot.modules.log_channel import loggable

# Reuse the tag-all member cache (extras.py) as the best available member list.
# The Bot API cannot enumerate all chat members, so candidates are the admins
# plus every user the bot has seen/cached in this chat.
try:
    from tg_bot.modules.extras import db as tag_db
except Exception:
    tag_db = None

LOGGER = logging.getLogger(__name__)

SCAN_DELAY = 0.2   # seconds between member lookups during a scan
KICK_DELAY = 0.5   # seconds between kicks to stay flood-safe


def _collect_zombies(bot: Bot, chat_id: int) -> List[int]:
    """Return the ids of deleted ("zombie") accounts still present in the chat.

    A deleted account is detected via the classic empty-first-name + no
    username heuristic (the same one global_bans.py uses).
    """
    zombies = []
    try:
        chat = bot.get_chat(chat_id)
    except TelegramError:
        return zombies

    candidates = []
    try:
        for admin in chat.get_administrators():
            candidates.append(admin.user.id)
    except TelegramError as err:
        LOGGER.warning("Could not fetch admins for %s: %s", chat_id, err)

    if tag_db:
        try:
            candidates.extend(uid for uid, _ in tag_db.get_users(chat_id))
        except Exception as err:
            LOGGER.warning("Could not read tag cache for %s: %s", chat_id, err)

    for uid in dict.fromkeys(candidates):  # dedupe, preserve order
        if uid == bot.id:
            continue
        try:
            member = chat.get_member(uid)
        except (BadRequest, TelegramError):
            continue
        if member.status in ("left", "kicked"):
            continue
        user = member.user
        if not user.is_bot and not user.first_name and not user.username:
            zombies.append(uid)
            sleep(SCAN_DELAY)

    return zombies


@run_async
@loggable
def _zombies_clean(bot: Bot, update: Update) -> str:
    chat = update.effective_chat
    message = update.effective_message
    user = message.from_user

    if not is_bot_admin(chat, bot.id):
        message.reply_text("I'm not admin!")
        return ""

    try:
        can_restrict = chat.get_member(bot.id).can_restrict_members
    except TelegramError:
        can_restrict = False
    if not can_restrict:
        message.reply_text("I can't restrict members here! Make sure I'm admin and can ban users.")
        return ""

    if not is_user_admin(chat, user.id):
        message.reply_text("Who dis non-admin telling me what to do?")
        return ""

    status = message.reply_text("Purging out zombies from this group...")
    zombies = _collect_zombies(bot, chat.id)
    if not zombies:
        status.edit_text("No zombies or deleted accounts found in this group, group is clean!")
        return ""

    killed, immune = 0, 0
    for uid in zombies:
        try:
            chat.kick_member(uid)
            killed += 1
            sleep(KICK_DELAY)
        except TelegramError:
            immune += 1

    if immune:
        status.edit_text(
            "Zombies killed: `{}`\n`{}` zombie(s) hold immunity.".format(killed, immune),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        status.edit_text("Zombies purged! Zombies killed: `{}`".format(killed), parse_mode=ParseMode.MARKDOWN)

    log = "<b>{}:</b>" \
          "\n#ZOMBIES" \
          "\n<b>Admin:</b> {}" \
          "\n<b>Zombies killed:</b> {}".format(html.escape(chat.title),
                                               mention_html(user.id, user.first_name),
                                               killed)
    if immune:
        log += "\n<b>Zombies with immunity:</b> {}".format(immune)
    return log


@run_async
def zombies(bot: Bot, update: Update, args: List[str]) -> None:
    chat = update.effective_chat
    message = update.effective_message

    if not chat or chat.type == "private":
        message.reply_text("This command only works in groups.")
        return

    if args and args[0].lower() == "clean":
        _zombies_clean(bot, update)
        return

    status = message.reply_text("Searching for zombies...")
    zombies = _collect_zombies(bot, chat.id)
    if not zombies:
        status.edit_text("No zombies or deleted accounts found in this group, group is clean!")
        return

    ids = ", ".join(str(z) for z in zombies)
    status.edit_text(
        "ALERT!!\n\n`{}` zombie(s) detected:\n`{}`\n\nClean them up with /zombies clean.".format(len(zombies), ids),
        parse_mode=ParseMode.MARKDOWN,
    )


ZOMBIE_HANDLER = CommandHandler("zombies", zombies, pass_args=True, filters=Filters.group)
dispatcher.add_handler(ZOMBIE_HANDLER)

__help__ = """
*Everyone:*
 - /zombies: Scans the group for zombies (deleted accounts) and reports how many were found.

*Admin only:*
 - /zombies clean: Kicks every deleted account (zombie) out of the group.
"""

__mod_name__ = "Zombies"
