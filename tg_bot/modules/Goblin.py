"""
Goblin — gobbles links and regurgitates media.

Detects social media URLs in group messages, downloads the media, and
replies with the file. YouTube links get an inline quality picker.
"""
import os
import re
import time
import string
import random
import logging
import tempfile
import threading
import urllib.parse
from typing import Dict, Optional, Tuple

import shutil
import cloudscraper
import yt_dlp
from telegram import (
    Update,
    Bot,
    ParseMode,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    run_async,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
)

from tg_bot import dispatcher
from tg_bot.modules.disable import DisableAbleCommandHandler

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform URL patterns
# ---------------------------------------------------------------------------

_YT_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:[^/\n\s]+/\S+/|(?:v|e(?:mbed)?)|\S*?[?&]v=)|youtu\.be/)([a-zA-Z0-9_-]{11})"
)
_TIKTOK_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:tiktok\.com/@\S+/video/\d+|vm\.tiktok\.com/\S+)"
)
_IG_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/(?:reel|p)/[A-Za-z0-9_-]+"
)
_X_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/\S+/status/\d+"
)
_REDDIT_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:redd\.it/\S+|reddit\.com/(?:r/\S+/comments|link)\S+)"
)
_FB_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:facebook\.com/\S+/videos|fb\.watch/\S+)"
)
_TWITCH_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?clips\.twitch\.tv/\S+"
)
_PIN_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:pinterest\.com/\S+|pin\.it/\S+)"
)
_THREADS_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:threads\.net|threads\.com)/@\S+/post/\S+"
)

ALL_PLATFORMS = {
    "youtube": _YT_PATTERN,
    "tiktok": _TIKTOK_PATTERN,
    "instagram": _IG_PATTERN,
    "x": _X_PATTERN,
    "reddit": _REDDIT_PATTERN,
    "facebook": _FB_PATTERN,
    "twitch": _TWITCH_PATTERN,
    "pinterest": _PIN_PATTERN,
    "threads": _THREADS_PATTERN,
}

# ---------------------------------------------------------------------------
# Cooldown and pending download state
# ---------------------------------------------------------------------------

_chat_cooldown: Dict[int, float] = {}
_COOLDOWN_SECONDS = 10

_pending: Dict[str, dict] = {}
_SHORT_ID_LEN = 8
_PENDING_TTL = 900  # 15 minutes

_pending_lock = threading.Lock()


def _gen_short_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=_SHORT_ID_LEN))


def _store_pending(info: dict) -> str:
    sid = _gen_short_id()
    with _pending_lock:
        _pending[sid] = {**info, "time": time.time()}
    return sid


def _pop_pending(sid: str) -> Optional[dict]:
    with _pending_lock:
        entry = _pending.pop(sid, None)
    if entry and (time.time() - entry.get("time", 0)) < _PENDING_TTL:
        return entry
    return None


def _cleanup_pending():
    now = time.time()
    with _pending_lock:
        expired = [k for k, v in _pending.items() if now - v.get("time", 0) > _PENDING_TTL]
        for k in expired:
            del _pending[k]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_urls(message) -> list:
    urls = []
    text = message.text or message.caption or ""
    for entity in (message.entities or []) + (message.caption_entities or []):
        if entity.type == "url":
            urls.append(text[entity.offset : entity.offset + entity.length])
        elif entity.type == "text_link":
            urls.append(entity.url)
    return urls


def _is_cooldown(chat_id: int) -> bool:
    last = _chat_cooldown.get(chat_id, 0)
    return (time.time() - last) < _COOLDOWN_SECONDS


def _set_cooldown(chat_id: int):
    _chat_cooldown[chat_id] = time.time()


def _detect_platform(url: str) -> Optional[str]:
    for name, pattern in ALL_PLATFORMS.items():
        if pattern.search(url):
            return name
    return None


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def _get_yt_formats(url: str) -> Tuple[Optional[dict], list]:
    """Extract YouTube info without downloading. Returns (info, format_list)."""
    try:
        ydl_opts = {
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None, []

        formats = info.get("formats", [])
        duration = info.get("duration") or 0
        title = info.get("title", "Unknown")
        thumb = info.get("thumbnail")

        return {
            "url": url,
            "title": title,
            "duration": duration,
            "thumbnail": thumb,
            "formats": formats,
        }, formats
    except Exception as err:
        LOGGER.warning("yt-dlp extract_info failed for %s: %s", url, err)
        return None, []


def _pick_format(formats: list, target: str) -> str:
    """Pick a yt-dlp format string from available formats."""
    if target == "a":
        # Audio only
        audio = [f for f in formats if f.get("acodec", "none") != "none" and f.get("vcodec", "none") == "none"]
        if audio:
            audio.sort(key=lambda f: f.get("abr", 0) or 0, reverse=True)
            return audio[0]["format_id"]
        return "bestaudio"

    # Video + audio
    heights = {"1": 1080, "2": 720, "3": 480}
    max_h = heights.get(target)

    if max_h:
        # Filter to formats <= target height that also have audio
        merged = [f for f in formats if (f.get("height") or 0) <= max_h and f.get("vcodec", "none") != "none"]
        if merged:
            merged.sort(key=lambda f: f.get("height", 0) or 0, reverse=True)
            vid_id = merged[0]["format_id"]
            # Find matching audio
            best_audio = [f for f in formats if f.get("acodec", "none") != "none" and f.get("vcodec", "none") == "none"]
            if best_audio:
                best_audio.sort(key=lambda f: f.get("abr", 0) or 0, reverse=True)
                return f"{vid_id}+{best_audio[0]['format_id']}"
            return vid_id
        # Fall through to best
        return "bestvideo+bestaudio/best"

    # target == "b" → best
    return "bestvideo+bestaudio/best"


def _download_yt(url: str, fmt_str: str, tmpdir: str) -> Optional[str]:
    """Download YouTube video with the given format string. Returns filepath or None."""
    try:
        ydl_opts = {
            "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
            "format": fmt_str,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "merge_output_format": "mp4",
            "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if not info:
            return None
        filepath = ydl.prepare_filename(info)
        # yt-dlp may change extension after merge
        if not os.path.exists(filepath):
            base, _ = os.path.splitext(filepath)
            for ext in (".mp4", ".mkv", ".webm", ".opus", ".mp3", ".m4a"):
                if os.path.exists(base + ext):
                    return base + ext
        return filepath if os.path.exists(filepath) else None
    except Exception as err:
        LOGGER.exception("yt-dlp download failed: %s", err)
        return None


def _download_generic(url: str, tmpdir: str, platform: str) -> Optional[str]:
    """Download media from any yt-dlp supported platform. Returns filepath or None."""
    try:
        ydl_opts = {
            "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
            "format": "best[ext=mp4][filesize<50M]/best[filesize<50M]/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "merge_output_format": "mp4",
            "max_filesize": 50 * 1024 * 1024,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if not info:
            return None
        filepath = ydl.prepare_filename(info)
        if not os.path.exists(filepath):
            base, _ = os.path.splitext(filepath)
            for ext in (".mp4", ".mkv", ".webm", ".opus", ".mp3", ".m4a"):
                if os.path.exists(base + ext):
                    return base + ext
        return filepath if os.path.exists(filepath) else None
    except Exception as err:
        LOGGER.warning("yt-dlp download failed for %s (%s): %s", url, platform, err)
        return None


def _download_threads(url: str, tmpdir: str) -> Optional[str]:
    """Scrape Threads (threads.net/threads.com) media via cloudscraper + OpenGraph."""
    try:
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(url, timeout=20)
        resp.raise_for_status()
        html = resp.text

        # Try og:video first, then og:image
        for prop in ("og:video", "og:image"):
            match = re.search(r'<meta\s+(?:property|name)=["\']' + prop + r'["\']\s+content=["\']([^"\']+)["\']', html)
            if not match:
                match = re.search(r'content=["\']([^"\']+)["\']\s+(?:property|name)=["\']' + prop + r'["\']', html)
            if match:
                media_url = match.group(1)
                if media_url.startswith("//"):
                    media_url = "https:" + media_url
                parsed = urllib.parse.urlparse(media_url)
                ext = os.path.splitext(parsed.path)[1] or ".mp4"
                filepath = os.path.join(tmpdir, "threads_media" + ext)
                dl_resp = scraper.get(media_url, timeout=30, stream=True)
                dl_resp.raise_for_status()
                with open(filepath, "wb") as f:
                    for chunk in dl_resp.iter_content(8192):
                        f.write(chunk)
                if os.path.getsize(filepath) > 50 * 1024 * 1024:
                    os.remove(filepath)
                    return None
                return filepath

        # Fallback: look for video_url or photo_url in JSON-LD
        json_match = re.search(r'"video_url"\s*:\s*"([^"]+)"', html)
        if not json_match:
            json_match = re.search(r'"display_url"\s*:\s*"([^"]+)"', html)
        if json_match:
            media_url = json_match.group(1).replace("\\u0026", "&")
            parsed = urllib.parse.urlparse(media_url)
            ext = os.path.splitext(parsed.path)[1] or ".mp4"
            filepath = os.path.join(tmpdir, "threads_media" + ext)
            dl_resp = scraper.get(media_url, timeout=30, stream=True)
            dl_resp.raise_for_status()
            with open(filepath, "wb") as f:
                for chunk in dl_resp.iter_content(8192):
                    f.write(chunk)
            if os.path.getsize(filepath) > 50 * 1024 * 1024:
                os.remove(filepath)
                return None
            return filepath

    except Exception as err:
        LOGGER.warning("Threads scrape failed for %s: %s", url, err)
    return None


# ---------------------------------------------------------------------------
# Send helpers
# ---------------------------------------------------------------------------

def _send_media(bot: Bot, chat_id: int, filepath: str, caption: str, reply_to: int) -> bool:
    """Send a media file to Telegram. Returns True on success."""
    size = os.path.getsize(filepath)
    if size > 50 * 1024 * 1024:
        return False

    ext = os.path.splitext(filepath)[1].lower()
    try:
        with open(filepath, "rb") as f:
            if ext in (".mp4", ".mkv", ".webm"):
                bot.send_video(chat_id, f, caption=caption, parse_mode=ParseMode.HTML, reply_to_message_id=reply_to, timeout=60)
            elif ext in (".mp3", ".m4a", ".opus", ".wav"):
                bot.send_audio(chat_id, f, caption=caption, parse_mode=ParseMode.HTML, reply_to_message_id=reply_to, timeout=60)
            elif ext in (".gif", ".png", ".jpg", ".jpeg", ".webp"):
                bot.send_photo(chat_id, f, caption=caption, parse_mode=ParseMode.HTML, reply_to_message_id=reply_to, timeout=60)
            else:
                bot.send_document(chat_id, f, caption=caption, parse_mode=ParseMode.HTML, reply_to_message_id=reply_to, timeout=60)
        return True
    except Exception as err:
        LOGGER.warning("Failed to send media: %s", err)
        return False


# ---------------------------------------------------------------------------
# YouTube quality picker callbacks
# ---------------------------------------------------------------------------

def _yt_quality_keyboard(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Best Quality", callback_data=f"gob:{sid}:b")],
            [
                InlineKeyboardButton("1080p", callback_data=f"gob:{sid}:1"),
                InlineKeyboardButton("720p", callback_data=f"gob:{sid}:2"),
            ],
            [
                InlineKeyboardButton("480p", callback_data=f"gob:{sid}:3"),
                InlineKeyboardButton("Audio Only", callback_data=f"gob:{sid}:a"),
            ],
        ]
    )


@run_async
def goblin_callback(bot: Bot, update: Update):
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    if not data.startswith("gob:"):
        return

    parts = data.split(":")
    if len(parts) != 3:
        return

    sid = parts[1]
    quality = parts[2]

    entry = _pop_pending(sid)
    if not entry:
        query.answer("This request expired. Send the link again.", show_alert=True)
        return

    query.answer("Downloading...")
    url = entry["url"]
    chat_id = entry["chat_id"]
    msg_id = entry["msg_id"]
    title = entry.get("title", "video")
    duration = entry.get("duration", 0)

    _set_cooldown(chat_id)

    fmt_str = _pick_format(entry.get("formats", []), quality)
    tmpdir = tempfile.mkdtemp(prefix="gob_yt_")
    try:
        bot.send_chat_action(chat_id, action="upload_video")
        filepath = _download_yt(url, fmt_str, tmpdir)
        if not filepath:
            query.edit_message_text("Download failed. Try a different quality.")
            return

        fsize = os.path.getsize(filepath)
        dur_str = f"{duration // 60}:{duration % 60:02d}" if duration else ""
        caption = "<b>{}</b>{}\n{}".format(
            _escape_html(title[:80]),
            f" ({dur_str})" if dur_str else "",
            _human_size(fsize),
        )

        query.edit_message_text("Uploading...")
        if _send_media(bot, chat_id, filepath, caption, msg_id):
            try:
                query.delete()
            except Exception:
                pass
        else:
            query.edit_message_text("File too large for Telegram (>50MB). Try a lower quality.")
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Main link detection handler
# ---------------------------------------------------------------------------

@run_async
def goblin_detect(bot: Bot, update: Update):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    # Only groups
    if chat.type == "private":
        return

    # Skip commands and bot messages
    if message.text and message.text.startswith("/"):
        return
    if message.from_user and message.from_user.is_bot:
        return

    # Cooldown
    if _is_cooldown(chat.id):
        return

    urls = _extract_urls(message)
    if not urls:
        return

    msg_id = message.message_id

    for url in urls:
        platform = _detect_platform(url)
        if not platform:
            continue

        _set_cooldown(chat.id)

        if platform == "youtube":
            _handle_youtube(bot, message, url, chat.id, msg_id)
        elif platform == "threads":
            _handle_threads(bot, message, url, chat.id, msg_id)
        else:
            _handle_generic(bot, message, url, chat.id, msg_id, platform)
        return  # one link per message


def _handle_youtube(bot: Bot, message, url: str, chat_id: int, msg_id: int):
    status = message.reply_text("Extracting video info...")
    try:
        info, formats = _get_yt_formats(url)
        if not info:
            status.edit_text("Could not extract video info.")
            return

        duration = info.get("duration", 0)
        if duration and duration > 30 * 60:
            status.edit_text("Video too long (>30 min). Send the link directly instead.")
            return

        title = info.get("title", "YouTube video")
        thumb = info.get("thumbnail")
        sid = _store_pending(info)

        title_short = title[:60] + ("..." if len(title) > 60 else "")
        dur_str = f"{duration // 60}:{duration % 60:02d}" if duration else ""
        text = "<b>{}</b>{}\nPick a quality:".format(
            _escape_html(title_short),
            f"\nDuration: {dur_str}" if dur_str else "",
        )
        status.edit_text(text, reply_markup=_yt_quality_keyboard(sid), parse_mode=ParseMode.HTML)

    except Exception as err:
        LOGGER.exception("YouTube handler error: %s", err)
        try:
            status.edit_text("Something went wrong.")
        except Exception:
            pass


def _handle_threads(bot: Bot, message, url: str, chat_id: int, msg_id: int):
    status = message.reply_text("Fetching Threads media...")
    tmpdir = tempfile.mkdtemp(prefix="gob_threads_")
    try:
        bot.send_chat_action(chat_id, action="upload_video")
        filepath = _download_threads(url, tmpdir)
        if not filepath:
            status.edit_text("Could not fetch media from Threads.")
            return

        fsize = os.path.getsize(filepath)
        caption = "<b>Threads</b>\n{}".format(_human_size(fsize))
        if _send_media(bot, chat_id, filepath, caption, msg_id):
            status.delete()
        else:
            status.edit_text("File too large for Telegram (>50MB).")
    except Exception as err:
        LOGGER.exception("Threads handler error: %s", err)
        try:
            status.edit_text("Something went wrong with Threads.")
        except Exception:
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _handle_generic(bot: Bot, message, url: str, chat_id: int, msg_id: int, platform: str):
    status = message.reply_text("Downloading from {}...".format(platform.title()))
    tmpdir = tempfile.mkdtemp(prefix="gob_{}_".format(platform))
    try:
        bot.send_chat_action(chat_id, action="upload_video")
        filepath = _download_generic(url, tmpdir, platform)
        if not filepath:
            status.edit_text("Could not download from {}.".format(platform.title()))
            return

        fsize = os.path.getsize(filepath)
        caption = "<b>{}</b>\n{}".format(
            _escape_html(platform.title()),
            _human_size(fsize),
        )
        if _send_media(bot, chat_id, filepath, caption, msg_id):
            status.delete()
        else:
            status.edit_text("File too large for Telegram (>50MB).")
    except Exception as err:
        LOGGER.exception("%s handler error: %s", platform, err)
        try:
            status.edit_text("Something went wrong with {}.".format(platform.title()))
        except Exception:
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Command handler (manual trigger)
# ---------------------------------------------------------------------------

@run_async
def goblin_cmd(bot: Bot, update: Update, args):
    msg = update.effective_message
    chat = update.effective_chat
    if not args:
        msg.reply_text("Usage: /goblin <url>")
        return
    url = " ".join(args).strip()
    platform = _detect_platform(url)
    if not platform:
        msg.reply_text("Unsupported platform.")
        return
    if platform == "youtube":
        _handle_youtube(bot, msg, url, chat.id, msg.message_id)
    elif platform == "threads":
        _handle_threads(bot, msg, url, chat.id, msg.message_id)
    else:
        _handle_generic(bot, msg, url, chat.id, msg.message_id, platform)


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

# Passive link detection (group=10 to not interfere with other handlers)
_link_handler = MessageHandler(
    Filters.group & (Filters.entity("url") | Filters.entity("text_link")),
    goblin_detect,
)
dispatcher.add_handler(_link_handler, group=10)

# Quality picker callback
dispatcher.add_handler(CallbackQueryHandler(goblin_callback, pattern=r"^gob:"))

# Manual command
_goblin_cmd = DisableAbleCommandHandler("goblin", goblin_cmd, pass_args=True)
dispatcher.add_handler(_goblin_cmd)

# Periodic cleanup of expired pending entries
def _periodic_cleanup():
    while True:
        time.sleep(300)
        _cleanup_pending()

_cleanup_thread = threading.Thread(target=_periodic_cleanup, daemon=True)
_cleanup_thread.start()

# ---------------------------------------------------------------------------
# Module metadata
# ---------------------------------------------------------------------------

__help__ = """
*Goblin*

Automatically grabs media from social links posted in groups.

*Supported platforms:*
YouTube, TikTok, Instagram, X (Twitter), Reddit, Facebook, Twitch Clips, Pinterest, Threads

*How it works:*
 - Just paste a link — the bot downloads and sends the media automatically.
 - YouTube links show a quality picker (Best / 1080p / 720p / 480p / Audio).
 - Other platforms download the best quality under 50MB.

*Manual trigger:*
 - /goblin <url>: Download a specific link on demand.

*Admin only:*
 - /disable goblin / /enable goblin: Toggle this command per chat.
"""

__mod_name__ = "Goblin"
