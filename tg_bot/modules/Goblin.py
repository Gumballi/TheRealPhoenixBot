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
import subprocess
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
_REDDIT_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:redd\.it/\S+|reddit\.com/(?:r/\S+/comments|link)\S+)"
)
_X_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/(?:\S+/status/\d+|i/status/\d+)"
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
_THREADSSHARE_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:threads\.net|threads\.com)/(?:@\S+/post/\S+|share/\S+)"
)

PLATFORMS = {
    "tiktok": _TIKTOK_PATTERN,
    "instagram": _IG_PATTERN,
    "x": _X_PATTERN,
    "reddit": _REDDIT_PATTERN,
    "facebook": _FB_PATTERN,
    "twitch": _TWITCH_PATTERN,
    "pinterest": _PIN_PATTERN,
    "threads": _THREADSSHARE_PATTERN,
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


def _content_type_rejects(response) -> bool:
    """Header-based pre-check: True means the response is clearly NOT real
    media (HTML/JSON error or block page) and shouldn't even be downloaded."""
    content_type = (response.headers.get("Content-Type") or "").lower()
    if not content_type:
        return False  # unknown - let the post-download file sniff decide
    if content_type.startswith(("image/", "video/", "audio/", "application/octet-stream")):
        return False
    return any(bad in content_type for bad in ("text/html", "application/json", "text/plain"))


def _validate_downloaded_file(filepath: str, min_bytes: int = 256) -> bool:
    """Guards against saving an HTML error/block/CAPTCHA page as if it were
    real image/video bytes. This was the direct cause of Telegram's
    'Image_process_failed' error: a Reddit block page got saved with a
    .jpeg extension and sent to Telegram as if it were a real photo.
    Sniffs the actual bytes written to disk rather than trusting the URL,
    extension, or an absent/misleading Content-Type header."""
    try:
        size = os.path.getsize(filepath)
    except OSError:
        return False
    if size < min_bytes:
        return False
    with open(filepath, "rb") as f:
        head = f.read(512)
    lowered = head.lstrip().lower()
    if lowered.startswith((b"<!doctype", b"<html", b"<?xml")) or lowered.startswith(b"{"):
        return False
    return True


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
                if _content_type_rejects(dl):
                    continue
                with open(filepath, "wb") as f:
                    for chunk in dl.iter_content(8192):
                        f.write(chunk)
                if os.path.getsize(filepath) > 50 * 1024 * 1024:
                    os.remove(filepath)
                    return None, meta
                if not _validate_downloaded_file(filepath):
                    os.remove(filepath)
                    continue
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
                if _content_type_rejects(dl):
                    continue
                with open(filepath, "wb") as f:
                    for chunk in dl.iter_content(8192):
                        f.write(chunk)
                if os.path.getsize(filepath) > 50 * 1024 * 1024:
                    os.remove(filepath)
                    return None, meta
                if not _validate_downloaded_file(filepath):
                    os.remove(filepath)
                    continue
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
    is_video = ext in (".mp4", ".mkv", ".webm")

    for attempt in range(3):
        try:
            with open(filepath, "rb") as f:
                if ext in (".mp3", ".m4a", ".opus", ".wav"):
                    bot.send_audio(chat_id, f, caption=caption, parse_mode=ParseMode.HTML,
                                   reply_to_message_id=reply_to, timeout=180)
                elif ext in (".gif", ".png", ".jpg", ".jpeg", ".webp"):
                    bot.send_photo(chat_id, f, caption=caption, parse_mode=ParseMode.HTML,
                                   reply_to_message_id=reply_to, timeout=60)
                elif is_video and size <= 50 * 1024 * 1024:
                    # Try send_video first (shows inline player)
                    bot.send_video(chat_id, f, caption=caption, parse_mode=ParseMode.HTML,
                                   reply_to_message_id=reply_to, timeout=180)
                else:
                    # Large videos or unknown types as document
                    bot.send_document(chat_id, f, caption=caption, parse_mode=ParseMode.HTML,
                                      reply_to_message_id=reply_to, timeout=180)
            return True
        except Exception as err:
            err_str = str(err)
            LOGGER.warning("Send attempt %d/3 failed (%s, %s): %s | %s",
                           attempt + 1, ext, _human_size(size), type(err).__name__, err_str[:200])
            # If send_video failed with "too large", try send_document as fallback.
            # If genuinely too large, no amount of retrying changes that - stop here
            # instead of burning the remaining retry attempts.
            if is_video and "too large" in err_str.lower():
                try:
                    with open(filepath, "rb") as f:
                        bot.send_document(chat_id, f, caption=caption, parse_mode=ParseMode.HTML,
                                          reply_to_message_id=reply_to, timeout=180)
                    return True
                except Exception as err2:
                    err2_str = str(err2)
                    LOGGER.warning("Document fallback also failed: %s | %s", type(err2).__name__, err2_str[:200])
                    if "too large" in err2_str.lower():
                        # Genuinely oversized (or a transient garbled-response false
                        # positive, e.g. during a deploy) - retrying send won't help.
                        return False
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
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


def _reddit_scrape(url: str, tmpdir: str) -> Tuple[Optional[str], dict]:
    """Scrape Reddit via old.reddit.com or RSS. Returns (filepath, metadata)."""
    meta = {}
    scraper = cloudscraper.create_scraper()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }

    # Normalize: strip tracking params, convert to old.reddit.com
    clean = url.split("?")[0]
    clean = clean.replace("www.reddit.com", "old.reddit.com")
    if not clean.endswith(".json"):
        clean = clean.rstrip("/") + ".json"

    # Try JSON API first
    try:
        resp = scraper.get(clean, timeout=15, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        post = data[0]["data"]["children"][0]["data"]
        meta["title"] = post.get("title", "")
        meta["uploader"] = post.get("author", "")

        if post.get("is_video"):
            video_url = post["media"]["reddit_video"]["fallback_url"].split("?")[0]
            audio_url = post["media"]["reddit_video"].get("fallback_audio_url", "")
            filepath = os.path.join(tmpdir, "reddit_video.mp4")
            dl = scraper.get(video_url, timeout=60, headers=headers, stream=True)
            dl.raise_for_status()
            if _content_type_rejects(dl):
                return None, meta
            with open(filepath, "wb") as f:
                for chunk in dl.iter_content(8192):
                    f.write(chunk)
            if os.path.getsize(filepath) > 50 * 1024 * 1024:
                os.remove(filepath)
                return None, meta
            if not _validate_downloaded_file(filepath):
                os.remove(filepath)
                return None, meta
            if audio_url:
                audio_path = os.path.join(tmpdir, "reddit_audio.mp4")
                try:
                    da = scraper.get(audio_url.split("?")[0], timeout=60, headers=headers, stream=True)
                    da.raise_for_status()
                    with open(audio_path, "wb") as f:
                        for chunk in da.iter_content(8192):
                            f.write(chunk)
                    merged = os.path.join(tmpdir, "reddit_merged.mp4")
                    try:
                        subprocess.run(
                            ["ffmpeg", "-y", "-i", filepath, "-i", audio_path, "-c", "copy", merged],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=60,
                            check=True,
                        )
                    except Exception as ffmpeg_err:
                        LOGGER.warning("ffmpeg merge failed: %s", ffmpeg_err)
                    if os.path.exists(merged) and os.path.getsize(merged) > 0:
                        os.remove(filepath)
                        filepath = merged
                except Exception:
                    pass
            return filepath, meta

        media_url = post.get("url_overridden_by_dest") or post.get("url", "")
        if not media_url:
            return None, meta
        parsed = urllib.parse.urlparse(media_url)
        ext = os.path.splitext(parsed.path)[1] or ".jpg"
        filepath = os.path.join(tmpdir, "reddit_media" + ext)
        dl = scraper.get(media_url, timeout=60, headers=headers, stream=True)
        dl.raise_for_status()
        if _content_type_rejects(dl):
            return None, meta
        with open(filepath, "wb") as f:
            for chunk in dl.iter_content(8192):
                f.write(chunk)
        if os.path.getsize(filepath) > 50 * 1024 * 1024:
            os.remove(filepath)
            return None, meta
        if not _validate_downloaded_file(filepath):
            os.remove(filepath)
            return None, meta
        return filepath, meta
    except Exception as err:
        LOGGER.info("Reddit JSON failed, trying RSS: %s", err)

    # Fallback: try the share endpoint (works when JSON is blocked)
    try:
        share_url = clean.replace(".json", "")
        resp = scraper.get(share_url, timeout=15, headers=headers)
        resp.raise_for_status()
        html = resp.text

        # Try to find og:video or og:image in the HTML
        for prop in ("og:video", "og:video:secure_url", "og:image"):
            m = re.search(r'property="og:(?:video|image)"[^>]+content="([^"]+)"', html) or \
                re.search(r'content="([^"]+)"[^>]+property="og:(?:video|image)"', html)
            if m:
                media_url = m.group(1)
                if media_url.startswith("//"):
                    media_url = "https:" + media_url
                parsed = urllib.parse.urlparse(media_url)
                ext = os.path.splitext(parsed.path)[1] or ".jpg"
                filepath = os.path.join(tmpdir, "reddit_share" + ext)
                dl = scraper.get(media_url, timeout=60, headers=headers, stream=True)
                dl.raise_for_status()
                if _content_type_rejects(dl):
                    continue
                with open(filepath, "wb") as f:
                    for chunk in dl.iter_content(8192):
                        f.write(chunk)
                if os.path.getsize(filepath) > 50 * 1024 * 1024:
                    os.remove(filepath)
                    return None, meta
                if not _validate_downloaded_file(filepath):
                    os.remove(filepath)
                    continue
                return filepath, meta

        # Try JSON-LD or embedded video tags
        for pattern in (
            r'"fallback_url"\s*:\s*"([^"]+)"',
            r'"contentUrl"\s*:\s*"([^"]+)"',
            r'<video[^>]+src="([^"]+)"',
        ):
            m = re.search(pattern, html)
            if m:
                media_url = m.group(1).replace("\\u0026", "&").replace("\\/", "/")
                if media_url.startswith("//"):
                    media_url = "https:" + media_url
                parsed = urllib.parse.urlparse(media_url)
                ext = os.path.splitext(parsed.path)[1] or ".mp4"
                filepath = os.path.join(tmpdir, "reddit_share" + ext)
                dl = scraper.get(media_url, timeout=60, headers=headers, stream=True)
                dl.raise_for_status()
                if _content_type_rejects(dl):
                    continue
                with open(filepath, "wb") as f:
                    for chunk in dl.iter_content(8192):
                        f.write(chunk)
                if os.path.getsize(filepath) > 50 * 1024 * 1024:
                    os.remove(filepath)
                    return None, meta
                if not _validate_downloaded_file(filepath):
                    os.remove(filepath)
                    continue
                return filepath, meta

        # Extract title from page
        title_m = re.search(r'<title>([^<]+)</title>', html)
        if title_m:
            meta["title"] = title_m.group(1).strip()[:100]
    except Exception as err:
        LOGGER.warning("Reddit share page scrape failed: %s", err)

    # Fallback: RSS feed
    try:
        rss_url = clean.replace(".json", ".rss")
        resp = scraper.get(rss_url, timeout=15, headers=headers)
        resp.raise_for_status()
        xml_text = resp.text

        # Extract title
        title_m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml_text)
        if title_m:
            meta["title"] = title_m.group(1).strip()

        # Find media:content or enclosure
        for pattern in (
            r'<media:content[^>]+url="([^"]+)"',
            r'<enclosure[^>]+url="([^"]+)"',
            r'<media:thumbnail[^>]+url="([^"]+)"',
        ):
            m = re.search(pattern, xml_text)
            if m:
                media_url = m.group(1)
                parsed = urllib.parse.urlparse(media_url)
                ext = os.path.splitext(parsed.path)[1] or ".jpg"
                filepath = os.path.join(tmpdir, "reddit_rss" + ext)
                dl = scraper.get(media_url, timeout=60, headers=headers, stream=True)
                dl.raise_for_status()
                if _content_type_rejects(dl):
                    continue
                with open(filepath, "wb") as f:
                    for chunk in dl.iter_content(8192):
                        f.write(chunk)
                if os.path.getsize(filepath) > 50 * 1024 * 1024:
                    os.remove(filepath)
                    return None, meta
                if not _validate_downloaded_file(filepath):
                    os.remove(filepath)
                    continue
                return filepath, meta
    except Exception as err:
        LOGGER.warning("Reddit RSS failed: %s", err)

    return None, meta


def _x_scrape(url: str, tmpdir: str) -> Tuple[Optional[str], dict]:
    """Scrape X/Twitter via syndication API. Returns (filepath, metadata)."""
    meta = {}
    try:
        # Extract tweet ID from URL
        m = re.search(r"/status/(\d+)", url)
        if not m:
            return None, meta
        tweet_id = m.group(1)

        # fxtwitter / vxtwitter public API
        scraper = cloudscraper.create_scraper()
        api_url = "https://api.fxtwitter.com/i/status/{}".format(tweet_id)
        resp = scraper.get(api_url, timeout=15, headers={"User-Agent": _UA})
        if resp.status_code != 200:
            # Try vxtwitter
            api_url = "https://api.vxtwitter.com/i/status/{}".format(tweet_id)
            resp = scraper.get(api_url, timeout=15, headers={"User-Agent": _UA})

        if resp.status_code != 200:
            return None, meta

        data = resp.json()
        tweet = data.get("tweet", data)
        meta["title"] = tweet.get("text", "")[:100]
        meta["uploader"] = tweet.get("author", {}).get("name", "")
        meta["uploader_url"] = tweet.get("author", {}).get("url", "")

        # Check for video
        videos = tweet.get("media", {}).get("videos", [])
        if videos:
            video_url = videos[0].get("url", "")
            if not video_url:
                return None, meta
            filepath = os.path.join(tmpdir, "x_video.mp4")
            dl = scraper.get(video_url, timeout=30, headers={"User-Agent": _UA}, stream=True)
            dl.raise_for_status()
            if _content_type_rejects(dl):
                return None, meta
            with open(filepath, "wb") as f:
                for chunk in dl.iter_content(8192):
                    f.write(chunk)
            if os.path.getsize(filepath) > 50 * 1024 * 1024:
                os.remove(filepath)
                return None, meta
            if not _validate_downloaded_file(filepath):
                os.remove(filepath)
                return None, meta
            return filepath, meta

        # Check for photos
        photos = tweet.get("media", {}).get("photos", [])
        if photos:
            photo_url = photos[0].get("url", "")
            if not photo_url:
                return None, meta
            filepath = os.path.join(tmpdir, "x_photo.jpg")
            dl = scraper.get(photo_url, timeout=30, headers={"User-Agent": _UA}, stream=True)
            dl.raise_for_status()
            if _content_type_rejects(dl):
                return None, meta
            with open(filepath, "wb") as f:
                for chunk in dl.iter_content(8192):
                    f.write(chunk)
            if not _validate_downloaded_file(filepath):
                os.remove(filepath)
                return None, meta
            return filepath, meta

        return None, meta
    except Exception as err:
        LOGGER.warning("X scrape failed: %s", err)
        return None, meta


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
            if platform == "reddit":
                status.edit_text("Trying Reddit scraper...")
                filepath, meta = _reddit_scrape(url, tmpdir)
            elif platform == "pinterest":
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
            elif platform == "x":
                status.edit_text("Trying X scraper...")
                filepath, meta = _x_scrape(url, tmpdir)

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
