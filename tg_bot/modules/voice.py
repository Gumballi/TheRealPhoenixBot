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
except ImportError:
    gTTS = None


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
    input_str = " ".join(args).strip()

    if reply and (reply.text or reply.caption):
        text = reply.text or reply.caption
        lan = input_str or "en"
    elif "-" in input_str:
        lan, text = input_str.split("-", 1)
    else:
        message.reply_text(
            "Usage: /voice <language code> - <text>\n"
            "Or reply to a message with: /voice <language code>\n\n"
            "Common codes: en, am, es, fr, de, hi, sw, ar, pt, ru"
        )
        return

    text = text.strip()
    lan = lan.strip()

    if not text:
        message.reply_text("Give me some text to speak!")
        return

    if gTTS is None:
        message.reply_text("gTTS is not installed. Add `gTTS` to requirements.txt and restart.")
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

    try:
        with open(opus_path, "rb") as voice_file:
            message.reply_voice(
                voice_file,
                caption=caption,
                reply_to_message_id=message.reply_to_message_id,
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
 - /voice <lang> (as a reply to a message): Speaks the replied message.

Language defaults to English if omitted.

Common language codes: `en` English, `am` Amharic, `es` Spanish, `fr` French,
`de` German, `hi` Hindi, `sw` Swahili, `ar` Arabic, `pt` Portuguese, `ru` Russian.

Full list: https://telegra.ph/SfMæisér--𐌷𐌴ࠋࠋ𐌱𐍈𐌸-𐌾𐌰𐍀𐌾-06-04
"""

__mod_name__ = "Voice"
