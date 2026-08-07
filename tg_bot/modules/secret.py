import uuid
import re
import html
import time
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.ext import CallbackQueryHandler, MessageHandler, Filters, InlineQueryHandler
from telegram.ext.dispatcher import run_async

from tg_bot import dispatcher, LOGGER
from tg_bot.modules.users import get_user_id

# Internal storage for active anonymous secrets
ANON_SECRET_DB = {}

# Secrets older than this are considered expired and pruned
SECRET_TTL_SECONDS = 86400  # 24 hours


def _prune_expired():
    now = time.time()
    expired = [k for k, v in ANON_SECRET_DB.items()
               if now - v["created_at"] > SECRET_TTL_SECONDS]
    for k in expired:
        ANON_SECRET_DB.pop(k, None)


def _truncate_for_alert(text, limit=190):
    """Truncate text to fit Telegram's 200-byte callback-alert limit."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore") + "..."


def _build_envelope(target_username, target_user_id, secret_id):
    """Build the envelope message + reveal button (used by both inline and message paths)."""
    keyboard = [[InlineKeyboardButton(text="Reveal Anonymous Secret", callback_data="anonsecret_{}".format(secret_id))]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # HTML + tg://user link: robust against underscores/formatting chars in usernames
    safe_name = html.escape(target_username, quote=True)
    text = (
        "<b>An Anonymous Secret Message Has Arrived!</b>\n\n"
        "<b>For:</b> <a href=\"tg://user?id={target_id}\">@{safe}</a>\n\n"
        "<i>Only the designated recipient can open this text frame.</i>"
    ).format(target_id=target_user_id, safe=safe_name)

    return text, reply_markup


def _answer_inline(bot, inline_query_id, title, description, text, reply_markup=None):
    article = InlineQueryResultArticle(
        id=str(uuid.uuid4())[:8],
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(text, parse_mode="HTML"),
        reply_markup=reply_markup,
    )
    bot.answer_inline_query(inline_query_id, results=[article], cache_time=0)


@run_async
def inline_secret_query(bot, update):
    """Handles inline mode: 'type @botname @target secret...' triggers this."""
    iq = update.inline_query
    if not iq or not iq.query:
        return

    bot_username = getattr(bot, "username", None) or "bot"
    query_text = iq.query.strip()

    # Query format (bot's username is stripped by Telegram, the rest arrives here):
    #   @TargetUsername <secret text>
    match = re.match(r"^@([A-Za-z0-9_]{5,32})(?:\s+(.+))?$", query_text, re.IGNORECASE | re.DOTALL)

    if not match:
        _answer_inline(
            bot, iq.id,
            "🔒 Send an anonymous secret",
            "Type: @{bot} @username your hidden message".format(bot=bot_username),
            "<b>How it works</b>\n\n"
            "Type <code>@{bot} @username your hidden message</code> and pick the result. "
            "Only the recipient can open the secret - nobody else can see it.".format(bot=bot_username),
        )
        return

    target_username = match.group(1)
    secret_text = (match.group(2) or "").strip()

    if not secret_text:
        _answer_inline(
            bot, iq.id,
            "Add your hidden message",
            "Type: @{bot} @username your secret text".format(bot=bot_username),
            "<b>Almost there!</b>\n\nAdd your secret text after the username.",
        )
        return

    target_user_id = get_user_id(target_username.lower())

    if not target_user_id:
        _answer_inline(
            bot, iq.id,
            "User not found",
            "@{target} hasn't messaged the bot yet".format(target=target_username),
            "<b>Error:</b> Could not find user <code>@{target}</code>.\n"
            "They must send at least one message to the bot first.".format(target=target_username),
        )
        return

    if target_user_id == iq.from_user.id:
        _answer_inline(
            bot, iq.id,
            "Can't send to yourself",
            "Pick a different recipient",
            "You can't send an anonymous secret message to yourself!",
        )
        return

    _prune_expired()

    # Generate a unique key for this secret instance
    secret_id = str(uuid.uuid4())[:8]

    ANON_SECRET_DB[secret_id] = {
        "text": secret_text,
        "target_id": target_user_id,
        "sender_id": iq.from_user.id,
        "created_at": time.time(),
        "revealed": False,
    }

    envelope, reply_markup = _build_envelope(target_username, target_user_id, secret_id)

    # The picked result IS the envelope: message text has no secret, button carries the key.
    _answer_inline(
        bot, iq.id,
        "🔒 Send anonymous secret to @{target}".format(target=target_username),
        "Only @{target} can open it - everyone else sees a locked envelope.".format(target=target_username),
        envelope,
        reply_markup=reply_markup,
    )


@run_async
def anonymous_secret_trigger(bot, update):
    """Fallback for when inline mode is disabled: '@botname @target secret' as a plain message."""
    message = update.effective_message
    if not message or not message.text:
        return

    # Grab bot username safely
    bot_username = getattr(bot, "username", None)
    if not bot_username:
        return

    # Pattern: @BotUsername @TargetUsername Secret Message
    pattern = rf"^@{re.escape(bot_username)}\s+@([A-Za-z0-9_]{{5,32}})\s+(.+)"
    match = re.match(pattern, message.text, re.IGNORECASE | re.DOTALL)

    if not match:
        return

    target_username = match.group(1)  # E.g. "yuri" (no "@")
    secret_text = match.group(2).strip()
    sender_user = message.from_user

    if not secret_text:
        return

    # Extract user ID from clean username using the repo's internal function
    target_user_id = get_user_id(target_username.lower())

    if not target_user_id:
        message.reply_text(
            "Error: Could not find user '@{target}'.\n"
            "Please ensure they have sent at least one message to the bot first.".format(target=target_username)
        )
        return

    if sender_user is None or target_user_id == sender_user.id:
        message.reply_text("You can't send an anonymous secret message to yourself!")
        return

    _prune_expired()

    secret_id = str(uuid.uuid4())[:8]
    ANON_SECRET_DB[secret_id] = {
        "text": secret_text,
        "target_id": target_user_id,
        "sender_id": sender_user.id,
        "created_at": time.time(),
        "revealed": False,
    }

    envelope, reply_markup = _build_envelope(target_username, target_user_id, secret_id)
    bot.send_message(message.chat.id, text=envelope, reply_markup=reply_markup, parse_mode="HTML")

    # Delete the triggering command so the raw text vanishes from the chat log
    try:
        message.delete()
    except Exception:
        pass


@run_async
def read_anonymous_secret(bot, update):
    query = update.callback_query
    user_id = query.from_user.id

    # Safely split on the first underscore to extract the exact uuid key
    parts = query.data.split("_", 1)
    if len(parts) != 2:
        query.answer(text="Error: Invalid secret reference.", show_alert=True)
        return
    secret_id = parts[1]

    secret_data = ANON_SECRET_DB.get(secret_id)

    if not secret_data:
        query.answer(text="Error: This anonymous secret message has expired or no longer exists.", show_alert=True)
        return

    # Hard expiry check on access
    if time.time() - secret_data["created_at"] > SECRET_TTL_SECONDS:
        ANON_SECRET_DB.pop(secret_id, None)
        query.answer(text="Error: This anonymous secret message has expired.", show_alert=True)
        return

    if user_id != secret_data["target_id"]:
        query.answer(text="Access Denied! This anonymous secret envelope belongs to someone else.", show_alert=True)
        return

    # One-time open: the envelope can only be decrypted once
    if secret_data.get("revealed"):
        query.answer(text="This anonymous secret has already been opened.", show_alert=True)
        return

    secret_text = _truncate_for_alert(secret_data["text"])

    try:
        query.answer(text="Decrypted Anonymous Message:\n\n{}".format(secret_text), show_alert=True)
    except Exception as e:
        LOGGER.warning("[anon_secret] Failed to reveal secret: %s", e)
        try:
            query.answer(text="Error: Could not reveal the secret right now. Please try again.", show_alert=True)
        except Exception:
            pass
        return

    secret_data["revealed"] = True


# Inline mode is on (enabled via BotFather), so the primary path is the inline query handler.
# The message handler below is kept as a fallback for when inline mode is turned off.
dispatcher.add_handler(InlineQueryHandler(inline_secret_query))
dispatcher.add_handler(
    MessageHandler(
        Filters.text & Filters.group,
        anonymous_secret_trigger
    )
)
dispatcher.add_handler(
    CallbackQueryHandler(
        read_anonymous_secret,
        pattern=r"^anonsecret_"
    )
)

__mod_name__ = "Anonymous Secrets"
__help__ = """
Send anonymous secret messages to users in a group using a mention trigger.

Usage (inline mode):
`@{bot_username} @username <your hidden text>`: start typing in the chat, then pick the
"Send anonymous secret" result. The envelope is posted with a reveal button - only the
recipient can open it.

Usage (inline mode disabled):
`@{bot_username} @username <your hidden text>`: sent as a normal message.

Notes:
- The recipient must have sent at least one message to the bot so the bot can look them up.
- Secrets expire after 24 hours and can only be opened once, by the recipient.
""".format(bot_username=getattr(dispatcher.bot, "username", None) or "bot")
