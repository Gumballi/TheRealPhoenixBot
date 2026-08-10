import datetime
import logging
import os
import subprocess
import tempfile
from typing import List

from telegram import Update, Bot
from telegram.ext import CommandHandler, Filters
from telegram.ext.dispatcher import run_async

from tg_bot import dispatcher

LOGGER = logging.getLogger(__name__)

try:
    from gtts import gTTS
    from gtts.langs import _langs
except ImportError:
    gTTS = None
    _langs = {}

COMMON_LANGS = {
    "af", "am", "ar", "bn", "bs", "ca", "cs", "cy", "da", "de",
    "el", "en", "es", "et", "eu", "fa", "fi", "fil", "fr", "fr-CA",
    "ga", "gl", "gu", "ha", "he", "hi", "hr", "hu", "hy", "id",
    "ig", "it", "ja", "jv", "ka", "kk", "km", "kn", "ko", "lo",
    "lt", "lv", "mg", "mk", "ml", "mr", "ms", "mt", "ne", "nl",
    "no", "pa", "pl", "pt", "ro", "ru", "si", "sk", "sl", "so",
    "sq", "sr", "sv", "sw", "ta", "te", "th", "tl", "tr", "uk",
    "ur", "uz", "vi", "yo", "zh-CN", "zh-TW", "zu",
}


def _parse_input(input_str: str, reply_text: str) -> tuple:
    input_str = input_str.strip()

    if not input_str:
        return ("en", reply_text) if reply_text else (None, None)

    if "-" in input_str:
        lan, text = input_str.split("-", 1)
        lan = lan.strip() or "en"
        text = text.strip() or (reply_text or "")
        return lan, text

    parts = input_str.split(None, 1)
    first = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""

    if first in COMMON_LANGS:
        if rest:
            return first, rest
        if reply_text:
            return first, reply_text
        return first, ""

    if reply_text and not rest:
        return "en", reply_text

    return "en", input_str


def _generate_voice(text: str, lan: str) -> tuple:
    tmp_dir = tempfile.mkdtemp(prefix="tts_")
    mp3_path = os.path.join(tmp_dir, "voice.mp3")
    opus_path = os.path.join(tmp_dir, "voice.opus")

    tts = gTTS(text, lang=lan)
    tts.save(mp3_path)

    command = [
        "ffmpeg", "-y",
        "-i", mp3_path,
        "-map", "0:a",
        "-codec:a", "libopus",
        "-b:a", "100k",
        "-vbr", "on",
        opus_path,
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return mp3_path, opus_path


@run_async
def voice(bot: Bot, update: Update, args: List[str]) -> None:
    message = update.effective_message
    reply = message.reply_to_message
    reply_text = reply.text or reply.caption if reply and (reply.text or reply.caption) else None
    input_str = " ".join(args).strip()

    lan, text = _parse_input(input_str, reply_text)

    if not text:
        message.reply_text(
            "Usage:\n"
            " /voice <language code> - <text>\n"
            " /voice <language code> <text>\n"
            " /voice <text>  (English by default)\n\n"
            "Or reply to a message with: /voice <language code>\n\n"
            "Common codes: en, am, es, fr, de, hi, sw, ar, pt, ru"
        )
        return

    if gTTS is None:
        message.reply_text("gTTS is not installed. Add `gTTS` to requirements.txt and restart.")
        return

    if lan not in _langs:
        message.reply_text(
            "Unsupported language code: `{}`\n\n"
            "Full list: https://telegra.ph/SfMæisér--𐌷𐌴ࠋࠋ𐌱𐍈𐌸-𐌾𐌰𐍀𐌾-06-04".format(lan)
        )
        return

    status = message.reply_text("Preparing voice...")
    start = datetime.datetime.now()

    mp3_path, opus_path = None, None
    try:
        mp3_path, opus_path = _generate_voice(text, lan)
    except Exception as err:
        LOGGER.exception("Voice generation failed")
        status.edit_text("Voice error: {}".format(err))
        return

    duration = (datetime.datetime.now() - start).seconds
    caption = "Voiced: {}...\nLanguage: {}\nTime taken: {}s".format(text[:97], lan, duration)
    reply_id = message.reply_to_message.message_id if message.reply_to_message else None

    try:
        with open(opus_path, "rb") as voice_file:
            message.reply_voice(
                voice_file,
                caption=caption,
                reply_to_message_id=reply_id,
            )
        status.delete()
    except Exception as err:
        LOGGER.exception("Failed to send voice note")
        status.edit_text("Failed to send voice: {}".format(err))
    finally:
        for path in (mp3_path, opus_path):
            if path and os.path.exists(path):
                os.remove(path)


VOICE_HANDLER = CommandHandler("voice", voice, pass_args=True)
dispatcher.add_handler(VOICE_HANDLER)

__help__ = """
*Text to Speech*
 - /voice <lang> - <text>: Generates a voice note speaking the given text.
 - /voice <lang> <text>: Same, without the `-` separator.
 - /voice <text>: Speaks in English by default.
 - /voice <lang> (as a reply to a message): Speaks the replied message.

Works with or without a reply. Language defaults to English if omitted.

The first word is auto-detected as the language if it matches a common
language code; otherwise the whole text is spoken in English. Use `-` to
force a specific code: `/voice <lang> - <text>`.

Common codes: `en` English, `am` Amharic, `es` Spanish, `fr` French,
`de` German, `hi` Hindi, `sw` Swahili, `ar` Arabic, `pt` Portuguese, `ru` Russian.

Full list: https://telegra.ph/SfMæisér--𐌷𐌴ࠋࠋ𐌱𐍈𐌸-𐌾𐌰𐍀𐌾-06-04
"""

__mod_name__ = "Voice"
