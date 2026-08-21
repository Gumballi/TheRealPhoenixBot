"""
Goblin -- gobbles links and regurgitates media.

Detects social media URLs in group messages, downloads the media, and
replies with the file.  Try yt-dlp first, then fall back to custom
cloudscraper scrapers for Pinterest / Instagram / Threads.
"""
import os
import re
import time
import logging
import tempfile
import shutil
import urllib.parse
from typing import Dict, Optional, Tuple

import cloudscraper
import yt_dlp
from telegram import (
    Update,
    Bot,
    ParseMode,
)
from telegram.ext import (
    run_async,
    MessageHandler,
    Filters,
)

from tg_bot import dispatcher
from tg_bot.modules.disable import DisableAbleCommandHandler

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform URL patterns
# ---------------------------------------------------------------------------

_TIKTOK_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:tiktok\.com/@\S+/video/\d+|vm\.tiktok\.com/\S+|vt\.tiktok\.com/\S+)"
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

PLATFORMS = {
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
# Helpers
# ---------------------------------------------------------------------------

_chat_cooldown: Dict[int, float] = {}
_COOLDOWN_SECONDS = 10

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


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
    return (time.time() - _chat_cooldown.get(chat_id, 0)) < _COOLDOWN_SECONDS


def _set_cooldown(chat_id: int):
    _chat_cooldown[chat_id] = time.time()


def _detect_platform(url: str) -> Optional[str]:
    for name, pattern in PLATFORMS.items():
        if pattern.search(url):
            return name
    return None


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Cloudscraper: OpenGraph meta-tag extraction (shared by Pinterest / IG / Threads)
# ---------------------------------------------------------------------------

def _og_scrape(url: str, props: tuple, prefix: str, tmpdir: str,
               json_fallbacks: tuple = ()) -> Tuple[Optional[str], dict]:
    """Generic OpenGraph scraper.  *props* are tried in order.
    Returns (filepath, metadata) or (None, {})."""
    meta = {}
    try:
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(url, timeout=20, headers={"User-Agent": _UA})
        resp.raise_for_status()
        html = resp.text
    except Exception as err:
        LOGGER.warning("%s scrape fetch failed for %s: %s", prefix, url, err)
        return None, meta

    # Extract metadata from OG tags
    for tag, key in (("og:title", "title"), ("og:description", "description"),
                     ("og:site_name", "source")):
        m = re.search(r'content=["\']([^"\']+)["\']', html.split('property="' + tag + '"')[1] if tag in html else "")
        if m:
            meta[key] = m.group(1).strip()

    # Also try twitter:author / profile:username for uploader
    for pattern in (r'"author"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"',
                    r'"uploader"\s*:\s*"([^"]+)"',
                    r'<meta\s+name=["\']twitter:title["\']\s+content=["\']([^"\']+)'):
        if "title" not in meta:
            m = re.search(pattern, html)
            if m:
                meta["title"] = m.group(1).strip()
                break

    for prop in props:
        for m in re.finditer(
            r'<meta\s+(?:property|name)=["\']' + re.escape(prop) + r'["\']\s+content=["\']([^"\']+)["\']',
            html,
        ) or re.finditer(
            r'content=["\']([^"\']+)["\']\s+(?:property|name)=["\']' + re.escape(prop) + r'["\']',
            html,
        ):
            media_url = m.group(1)
            if media_url.startswith("//"):
                media_url = "https:" + media_url
            parsed = urllib.parse.urlparse(media_url)
            is_video = "video" in prop
            ext = os.path.splitext(parsed.path)[1] or (".mp4" if is_video else ".jpg")
            filepath = os.path.join(tmpdir, f"{prefix}_media{ext}")
            try:
                dl = scraper.get(media_url, timeout=30, stream=True)
                dl.raise_for_status()
                with open(filepath, "wb") as f:
                    for chunk in dl.iter_content(8192):
                        f.write(chunk)
                if os.path.getsize(filepath) > 50 * 1024 * 1024:
                    os.remove(filepath)
                    return None, meta
                return filepath, meta
            except Exception:
                continue

    for pattern in json_fallbacks:
        m = re.search(pattern, html)
        if m:
            media_url = m.group(1).replace("\\u0026", "&").replace("\\/", "/")
            if media_url.startswith("//"):
                media_url = "https:" + media_url
            parsed = urllib.parse.urlparse(media_url)
            ext = os.path.splitext(parsed.path)[1] or ".mp4"
            filepath = os.path.join(tmpdir, f"{prefix}_media{ext}")
            try:
                dl = scraper.get(media_url, timeout=30, stream=True)
                dl.raise_for_status()
                with open(filepath, "wb") as f:
                    for chunk in dl.iter_content(8192):
                        f.write(chunk)
                if os.path.getsize(filepath) > 50 * 1024 * 1024:
                    os.remove(filepath)
                    return None, meta
                return filepath, meta
            except Exception:
                continue

    return None, meta


# ---------------------------------------------------------------------------
# yt-dlp: generic download
# ---------------------------------------------------------------------------

def _download_ytdlp(url: str, tmpdir: str, platform: str) -> Tuple[Optional[str], dict]:
    """Try yt-dlp.  Returns (filepath, metadata) or (None, {})."""
    meta = {}
    try:
        ydl_opts = {
            "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
            "format": "best[ext=mp4]/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "merge_output_format": "mp4",
            "extractor_args": {},
        }
        # TikTok needs specific client on datacenter IPs
        if platform == "tiktok":
            ydl_opts["extractor_args"]["tiktok"] = {"player_client": ["web"]}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if not info:
            return None, meta

        # Extract metadata
        for key in ("title", "uploader", "uploader_url", "description", "duration"):
            val = info.get(key)
            if val:
                meta[key] = val
        if info.get("thumbnail"):
            meta["thumbnail"] = info["thumbnail"]

        filepath = ydl.prepare_filename(info)
        if not os.path.exists(filepath):
            base, _ = os.path.splitext(filepath)
            for ext in (".mp4", ".mkv", ".webm", ".opus", ".mp3", ".m4a"):
                if os.path.exists(base + ext):
                    filepath = base + ext
                    break
        if not os.path.exists(filepath):
            return None, meta

        # Verify size on disk (Telegram limit is ~50MB for video)
        if os.path.getsize(filepath) > 50 * 1024 * 1024:
            os.remove(filepath)
            return None, meta

        return filepath, meta
    except Exception as err:
        LOGGER.warning("yt-dlp failed for %s (%s): %s", url, platform, err)
        return None, meta


# ---------------------------------------------------------------------------
# Send helper
# ---------------------------------------------------------------------------

def _send_media(bot: Bot, chat_id: int, filepath: str, caption: str, reply_to: int) -> bool:
    size = os.path.getsize(filepath)
    ext = os.path.splitext(filepath)[1].lower()
    try:
        with open(filepath, "rb") as f:
            if ext in (".mp3", ".m4a", ".opus", ".wav"):
                bot.send_audio(chat_id, f, caption=caption, parse_mode=ParseMode.HTML,
                               reply_to_message_id=reply_to, timeout=120)
            elif ext in (".gif", ".png", ".jpg", ".jpeg", ".webp"):
                bot.send_photo(chat_id, f, caption=caption, parse_mode=ParseMode.HTML,
                               reply_to_message_id=reply_to, timeout=60)
            else:
                # Videos as document — avoids send_video 50MB cap + upload quirks
                bot.send_document(chat_id, f, caption=caption, parse_mode=ParseMode.HTML,
                                  reply_to_message_id=reply_to, timeout=120)
        return True
    except Exception as err:
        LOGGER.warning("Failed to send media (%s, %s): %s", ext, _human_size(size), err)
        return False


# ---------------------------------------------------------------------------
# Platform handlers
# ---------------------------------------------------------------------------

def _build_caption(bot: Bot, platform: str, meta: dict) -> str:
    """Build an HTML caption from metadata + bot username."""
    parts = []

    # Platform header
    parts.append("<b>{}</b>".format(_escape_html(platform.title())))

    # Title
    if meta.get("title"):
        parts.append(_escape_html(meta["title"][:100]))

    # Uploader / author
    if meta.get("uploader"):
        author = meta["uploader"]
        if meta.get("uploader_url"):
            author = '<a href="{}">{}</a>'.format(meta["uploader_url"], _escape_html(author))
        parts.append("{}".format(author))

    # Duration
    dur = meta.get("duration")
    if dur:
        parts.append("{}:{}".format(dur // 60, dur % 60))

    # Description (truncated)
    desc = meta.get("description", "")
    if desc and len(desc) > 200:
        desc = desc[:200] + "..."
    if desc:
        parts.append(_escape_html(desc))

    # Bot username
    try:
        bot_user = getattr(bot, "username", None) or "Phoenix"
    except Exception:
        bot_user = "Phoenix"
    parts.append("@{}".format(bot_user))

    return "\n".join(parts)


def _handle_threads(bot: Bot, message, url: str, chat_id: int, msg_id: int):
    status = message.reply_text("Fetching Threads media...")
    tmpdir = tempfile.mkdtemp(prefix="gob_threads_")
    try:
        bot.send_chat_action(chat_id, action="upload_video")
        filepath, meta = _og_scrape(
            url,
            props=("og:video", "og:image"),
            prefix="threads",
            tmpdir=tmpdir,
            json_fallbacks=(
                r'"video_url"\s*:\s*"([^"]+)"',
                r'"display_url"\s*:\s*"([^"]+)"',
            ),
        )
        if not filepath:
            status.edit_text("Could not fetch media from Threads.")
            return
        meta.setdefault("title", "Threads")
        caption = _build_caption(bot, "threads", meta)
        if _send_media(bot, chat_id, filepath, caption, msg_id):
            status.delete()
        else:
            status.edit_text("Failed to upload to Telegram — try again later.")
    except Exception as err:
        LOGGER.exception("Threads handler error: %s", err)
        try:
            status.edit_text("Something went wrong with Threads.")
        except Exception:
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _handle_generic(bot: Bot, message, url: str, chat_id: int, msg_id: int, platform: str):
    """Generic handler: try yt-dlp, then custom scraper for known platforms."""
    status = message.reply_text("Downloading from {}...".format(platform.title()))
    tmpdir = tempfile.mkdtemp(prefix="gob_{}_".format(platform))
    try:
        bot.send_chat_action(chat_id, action="upload_video")
        meta = {}

        # 1) yt-dlp
        filepath, meta = _download_ytdlp(url, tmpdir, platform)

        # 2) Custom scrapers when yt-dlp fails
        if not filepath:
            if platform == "pinterest":
                status.edit_text("Trying Pinterest scraper...")
                filepath, meta = _og_scrape(
                    url,
                    props=("og:video", "og:video:secure_url", "og:image"),
                    prefix="pinterest",
                    tmpdir=tmpdir,
                    json_fallbacks=(
                        r'"video_url"\s*:\s*"([^"]+)"',
                        r'"embed_url"\s*:\s*"([^"]+)"',
                    ),
                )
            elif platform == "instagram":
                status.edit_text("Trying Instagram scraper...")
                filepath, meta = _og_scrape(
                    url,
                    props=("og:video", "og:video:secure_url", "og:image"),
                    prefix="instagram",
                    tmpdir=tmpdir,
                    json_fallbacks=(
                        r'"video_url"\s*:\s*"([^"]+)"',
                        r'"display_url"\s*:\s*"([^"]+)"',
                    ),
                )

        if not filepath:
            # TikTok photo posts (slideshows) can't be downloaded as video
            if platform == "tiktok":
                status.edit_text("This looks like a TikTok photo post — can't download as video.")
            else:
                status.edit_text("Could not download from {}.".format(platform.title()))
            return

        caption = _build_caption(bot, platform, meta)
        if _send_media(bot, chat_id, filepath, caption, msg_id):
            status.delete()
        else:
            status.edit_text("Failed to upload to Telegram — try again later.")
    except Exception as err:
        LOGGER.exception("%s handler error: %s", platform, err)
        try:
            status.edit_text("Something went wrong with {}.".format(platform.title()))
        except Exception:
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main link detection
# ---------------------------------------------------------------------------

@run_async
def goblin_detect(bot: Bot, update: Update):
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if chat.type == "private":
        return
    if message.text and message.text.startswith("/"):
        return
    if message.from_user and message.from_user.is_bot:
        return
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

        if platform == "threads":
            _handle_threads(bot, message, url, chat.id, msg_id)
        else:
            _handle_generic(bot, message, url, chat.id, msg_id, platform)
        return  # one link per message


# ---------------------------------------------------------------------------
# /goblin command
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
    if platform == "threads":
        _handle_threads(bot, msg, url, chat.id, msg.message_id)
    else:
        _handle_generic(bot, msg, url, chat.id, msg.message_id, platform)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

dispatcher.add_handler(
    MessageHandler(
        Filters.group & (Filters.entity("url") | Filters.entity("text_link")),
        goblin_detect,
    ),
    group=10,
)
dispatcher.add_handler(DisableAbleCommandHandler("goblin", goblin_cmd, pass_args=True))

# ---------------------------------------------------------------------------
# Module metadata
# ---------------------------------------------------------------------------

__help__ = """
*Goblin*

Automatically grabs media from social links posted in groups.

*Supported platforms:*
TikTok, Instagram, X (Twitter), Reddit, Facebook, Twitch Clips, Pinterest, Threads

*How it works:*
 - Just paste a link and the bot grabs the media automatically.
 - Downloads the best quality under 50MB with title, uploader, and description.

*Manual trigger:*
 - /goblin <url>: Download a specific link on demand.

*Admin only:*
 - /disable goblin / /enable goblin: Toggle this command per chat.
"""

__mod_name__ = "Goblin"
