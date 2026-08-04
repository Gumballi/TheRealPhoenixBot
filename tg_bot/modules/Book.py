import glob
import html
import io
import logging
import os
import re
import shutil
import tempfile
import urllib.parse
from typing import List, Optional

import requests
import cloudscraper
from telegram import Bot, ParseMode, Update
from telegram.ext import run_async
from torrentp import TorrentDownloader

from tg_bot import dispatcher
from tg_bot.modules.disable import DisableAbleCommandHandler

LOGGER = logging.getLogger(__name__)

# Create a global scraper configured to perfectly mimic a real desktop browser
# This bypasses the 403 Forbidden blocks caused by Cloudflare.
SCRAPER = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)

# ==========================================
# GUTENDEX (PUBLIC DOMAIN) LOGIC
# ==========================================

class BookFetcher:
    FORMAT_PRIORITY = [
        ("application/epub+zip", "epub"),
        ("application/x-mobipocket-ebook", "mobi"),
        ("application/pdf", "pdf"),
        ("text/plain; charset=utf-8", "txt"),
    ]

    @staticmethod
    def search_books(query: str) -> Optional[dict]:
        url = "https://gutendex.com/books"
        
        try:
            # Using SCRAPER instead of requests
            resp = SCRAPER.get(url, params={"search": query}, timeout=15)
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            LOGGER.warning(f"[book] Gutendex timed out for '{query}'")
            return None
        except requests.exceptions.RequestException as e:
            LOGGER.warning(f"[book] Gutendex search failed for '{query}': {e!r}")
            return None

        try:
            results = resp.json().get("results", [])
        except ValueError:
            LOGGER.warning(f"[book] Gutendex returned non-JSON for '{query}'")
            return None

        for book in results:
            formats = book.get("formats", {})
            dl_url = None
            for mime, _ext in BookFetcher.FORMAT_PRIORITY:
                if mime in formats:
                    dl_url = formats[mime]
                    break
            if not dl_url:
                continue

            authors = ", ".join(a.get("name", "") for a in book.get("authors", []))
            return {
                "title": book.get("title", "Unknown Title"),
                "author": authors or "Unknown Author",
                "download_url": dl_url,
                "cover": formats.get("image/jpeg"),
            }

        return {}


@run_async
def book(bot: Bot, update: Update, args: List[str]):
    msg = update.effective_message
    query = " ".join(args).strip()

    if not query:
        msg.reply_text(
            "Please provide a book title or title and author!\n<b>Usage:</b> <code>/book Pride and Prejudice</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    status_msg = msg.reply_text(
        f"Searching public domain for: <b>{html.escape(query)}</b>...", parse_mode=ParseMode.HTML
    )

    book_info = BookFetcher.search_books(query)

    if book_info is None:
        status_msg.edit_text("Search failed — Gutendex didn't respond. Try again in a moment.")
        return

    if not book_info.get("download_url"):
        status_msg.edit_text("No downloadable public domain book found for your query.")
        return

    title = book_info["title"]
    author = book_info["author"]
    dl_url = book_info["download_url"]
    cover_url = book_info.get("cover")

    status_msg.edit_text(
        f"Found <b>{html.escape(title)}</b> by <i>{html.escape(author)}</i>\nDownloading and sending file to you...",
        parse_mode=ParseMode.HTML,
    )

    try:
        # Use SCRAPER to download the file so Cloudflare doesn't block the download itself
        resp = SCRAPER.get(dl_url, timeout=30)
        resp.raise_for_status()
        content = resp.content

        if "epub" in dl_url:
            ext = "epub"
        elif "pdf" in dl_url:
            ext = "pdf"
        elif "mobi" in dl_url:
            ext = "mobi"
        else:
            ext = "txt"

        file_obj = io.BytesIO(content)
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title).replace(" ", "_")
        file_obj.name = f"{safe_title}.{ext}"
        file_obj.seek(0)

        thumb_obj = None
        if cover_url:
            try:
                # Use SCRAPER for the thumbnail too
                thumb_resp = SCRAPER.get(cover_url, timeout=10)
                thumb_resp.raise_for_status()
                thumb_obj = io.BytesIO(thumb_resp.content)
                thumb_obj.name = "cover.jpg"
                thumb_obj.seek(0)
            except requests.exceptions.RequestException as thumb_err:
                LOGGER.warning(f"[book] Failed to download thumbnail for {title}: {thumb_err}")

        msg.reply_document(
            document=file_obj,
            thumb=thumb_obj,
            caption=(
                f"<b>{html.escape(title)}</b>\nAuthor: {html.escape(author)}\n\n"
                f"Downloaded via @{bot.username}"
            ),
            parse_mode=ParseMode.HTML,
        )

        try:
            status_msg.delete()
        except Exception as del_err:
            LOGGER.warning(f"[book] Could not delete status message: {del_err!r}")

    except requests.exceptions.RequestException as e:
        LOGGER.error(f"[book] Download error: {e!r}")
        status_msg.edit_text(
            f"Failed to download file. Direct link: <a href='{html.escape(dl_url)}'>Download Book</a>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        LOGGER.error(f"[book] Unexpected error sending document: {e!r}")
        status_msg.edit_text("Something went wrong sending the file. Check logs.")


# ==========================================
# PIRATE BAY (TORRENT) LOGIC
# ==========================================

TRACKERS = "&tr=" + "&tr=".join([
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
])

class TPBDownloader:
    @staticmethod
    def get_best_magnet(query: str) -> Optional[dict]:
        # --- Attempt 1: SolidTorrents/BitSearch ---
        try:
            # We noticed SolidTorrents redirected to BitSearch in your logs.
            # Using SCRAPER to bypass the Cloudflare 403 blocks.
            st_url = "https://bitsearch.to/api/v1/search"
            st_resp = SCRAPER.get(st_url, params={"q": query, "category": "all"}, timeout=15)
            st_resp.raise_for_status()
            
            st_data = st_resp.json()
            # Bitsearch JSON structure is slightly different, it uses 'data' instead of 'results'
            results = st_data.get("data", []) if "data" in st_data else st_data.get("results", [])
            
            if results:
                top = results[0]
                # BitSearch uses 'magnet' or constructs it from info_hash
                info_hash = top.get("info_hash")
                name = top.get("name") or top.get("title", "Unknown_Torrent")
                
                if info_hash:
                    magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(name)}{TRACKERS}"
                    return {"name": name, "magnet": magnet}
        except requests.exceptions.Timeout:
            LOGGER.warning(f"[BitSearch] Timed out for '{query}'")
        except requests.exceptions.RequestException as e:
            LOGGER.warning(f"[BitSearch] Search failed for '{query}': {e!r}")
        except ValueError:
            LOGGER.warning(f"[BitSearch] Returned non-JSON for '{query}'")

        # --- Attempt 2: Pirate Bay via CodeTabs Proxy (Fallback) ---
        try:
            target_url = f"https://apibay.org/q.php?q={urllib.parse.quote(query)}&cat=0"
            # Swapped AllOrigins for CodeTabs to bypass the 520 Error
            proxy_url = f"https://api.codetabs.com/v1/proxy?quest={urllib.parse.quote(target_url)}"
            
            pb_resp = SCRAPER.get(proxy_url, timeout=20)
            pb_resp.raise_for_status()
            
            pb_data = pb_resp.json()
            if pb_data and isinstance(pb_data, list) and pb_data[0].get("id") != "0":
                top = pb_data[0]
                info_hash = top.get("info_hash")
                name = top.get("name", "Unknown_Torrent")
                
                magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(name)}{TRACKERS}"
                return {"name": name, "magnet": magnet}
                
        except requests.exceptions.Timeout:
            LOGGER.warning(f"[TPB Proxy] Timed out for '{query}'")
            return None
        except requests.exceptions.RequestException as e:
            safe_text = str(e).replace("<", "[").replace(">", "]")
            LOGGER.error(f"[TPB Proxy] Search failed: {safe_text}")
            return None
        except ValueError:
            safe_text = pb_resp.text[:200].replace("<", "[").replace(">", "]")
            LOGGER.warning(f"[TPB Proxy] Returned non-JSON for '{query}'. Response: {safe_text}")
            return None
            
        return {}


@run_async
def piratebook(bot: Bot, update: Update, args: List[str]):
    msg = update.effective_message
    query = " ".join(args).strip()

    if not query:
        msg.reply_text(
            "Please specify a book title!\n<b>Usage:</b> <code>/piratebook 1984 George Orwell</code>", 
            parse_mode=ParseMode.HTML
        )
        return

    status_msg = msg.reply_text(
        f"Searching torrents for: <b>{html.escape(query)}</b>...", 
        parse_mode=ParseMode.HTML
    )

    result = TPBDownloader.get_best_magnet(query)
    
    if result is None:
        status_msg.edit_text("Search failed — torrent trackers didn't respond. Try again in a moment.")
        return
        
    if not result:
        status_msg.edit_text("No ebook torrents found for that query.")
        return

    torrent_name = result["name"]
    magnet_link = result["magnet"]

    status_msg.edit_text(
        f"Downloading torrent: <b>{html.escape(torrent_name)}</b>...\n<i>Please wait, this depends on active seeders.</i>", 
        parse_mode=ParseMode.HTML
    )

    temp_dir = tempfile.mkdtemp()
    try:
        downloader = TorrentDownloader(magnet_link, temp_dir)
        downloader.start_download()

        found_files = []
        for ext in ['*.epub', '*.pdf', '*.mobi', '*.azw3']:
            found_files.extend(glob.glob(os.path.join(temp_dir, '**', ext), recursive=True))

        if not found_files:
            found_files = [f for f in glob.glob(os.path.join(temp_dir, '**', '*'), recursive=True) if os.path.isfile(f)]

        if not found_files:
            status_msg.edit_text("Download timed out or no file was extracted.")
            return

        target_file = found_files[0]
        file_size_mb = round(os.path.getsize(target_file) / (1024 * 1024), 2)

        if file_size_mb > 50:
            status_msg.edit_text(f"Warning: File size ({file_size_mb} MB) exceeds Telegram's 50MB bot upload limit.")
            return

        status_msg.edit_text("Uploading book to Telegram...")

        with open(target_file, "rb") as doc:
            msg.reply_document(
                document=doc,
                caption=f"<b>{html.escape(torrent_name)}</b>\nSize: <code>{file_size_mb} MB</code>\n\nDownloaded via @{bot.username}",
                parse_mode=ParseMode.HTML
            )
            
        try:
            status_msg.delete()
        except Exception as del_err:
            LOGGER.warning(f"[piratebook] Could not delete status message: {del_err!r}")

    except Exception as e:
        LOGGER.error(f"[TPB Download Error]: {e!r}")
        status_msg.edit_text(
            f"Failed to download torrent file.\n<b>Error:</b> <code>{html.escape(str(e))}</code>", 
            parse_mode=ParseMode.HTML
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ==========================================
# MODULE REGISTRATION
# ==========================================

__help__ = """
Download ebooks directly to Telegram.

*Available commands:*
 - /book <title or author>: Searches the Gutendex API for free public domain books and sends the file directly.
 - /piratebook <title/author>: Searches torrent indexers, downloads the ebook, and uploads it to chat.
"""

__mod_name__ = "Books"

BOOK_HANDLER = DisableAbleCommandHandler("book", book, pass_args=True)
PIRATEBOOK_HANDLER = DisableAbleCommandHandler("piratebook", piratebook, pass_args=True)

dispatcher.add_handler(BOOK_HANDLER)
dispatcher.add_handler(PIRATEBOOK_HANDLER)
