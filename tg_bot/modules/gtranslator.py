import os
import requests
from telegram import ParseMode, Update, Bot
from tg_bot import dispatcher
from tg_bot.modules.disable import DisableAbleCommandHandler

LIBRE_URL = os.environ.get("LIBRETRANSLATE_URL", "https://lt.vern.cc")


def _detect(text: str) -> str:
    try:
        r = requests.post(
            f"{LIBRE_URL}/detect",
            json={"q": text},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()[0]["language"]
    except Exception:
        return "auto"


def _translate(text: str, source: str = "auto", target: str = "en") -> str:
    r = requests.post(
        f"{LIBRE_URL}/translate",
        json={"q": text, "source": source, "target": target},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["translatedText"]


def translate(bot: Bot, update: Update) -> None:
    message = update.effective_message
    reply_msg = message.reply_to_message
    if not reply_msg:
        message.reply_text("Reply to a message to translate it!")
        return
    if reply_msg.caption:
        to_translate = reply_msg.caption
    elif reply_msg.text:
        to_translate = reply_msg.text
    else:
        message.reply_text("No text found to translate!")
        return
    try:
        args = message.text.split()[1].lower()
        if "//" in args:
            source = args.split("//")[0]
            dest = args.split("//")[1]
        else:
            source = _detect(to_translate)
            dest = args
    except IndexError:
        source = _detect(to_translate)
        dest = "en"
    try:
        translation = _translate(to_translate, source=source, target=dest)
    except Exception as e:
        message.reply_text(f"Translation failed: {e}")
        return
    reply = f"<b>Translated from {source} to {dest}</b>:\n" \
        f"<code>{translation}</code>"

    message.reply_text(reply, parse_mode=ParseMode.HTML)


def languages(bot: Bot, update: Update) -> None:
    message = update.effective_message
    message.reply_text(
        "Click [here](https://cloud.google.com/translate/docs/languages)"
        " to see the list of supported language codes!",
        disable_web_page_preview=True, parse_mode=ParseMode.MARKDOWN)


__mod_name__ = "Translation"

__help__ = """
Use this module to translate stuff... duh!

*Commands:*
• `/tl` (or `/tr`): as a reply to a message, translates it to English.

• `/tl <lang>`: translates to <lang>
eg: `/tl ja`: translates to Japanese.

• `/tl <source>//<dest>`: translates from <source> to <lang>.
eg: `/tl ja//en`: translates from Japanese to English.

• `/langs`: get a list of supported languages for translation."""


TRANSLATE_HANDLER = DisableAbleCommandHandler(["tl", "tr"], translate)
LANGUAGES_HANDLER = DisableAbleCommandHandler("langs", languages)
dispatcher.add_handler(TRANSLATE_HANDLER)
dispatcher.add_handler(LANGUAGES_HANDLER)
