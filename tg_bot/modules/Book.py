import os
import glob
import tempfile
import io
import re
import html
import urllib.parse
import requests
import logging
from typing import List

from telegram import Bot, Update, ParseMode
from telegram.ext import run_async
from torrentp import TorrentDownloader

from tg_bot import dispatcher
from tg_bot.modules.disable import DisableAbleCommandHandler

LOGGER = logging.getLogger(__name__)

# ==========================================
# GUTENDEX (PUBLIC DOMAIN) LOGIC
# ==========================================

class BookFetcher:
    @staticmethod
    def search_books(query: str) -> dict:
        """
        Searches Gutendex API for free public-domain books and direct downloads.
        """
        url = "https://gutendex.com/books"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; PhoenixBookBot/1.0)"}
        
        try:
            resp = requests.get(url, params={"search": query}, headers=headers, timeout=12)
            
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                
                if results:
                    book = results[0]
                    formats = book.get("formats", {})
                    
                    # Look for epub or pdf or mobi download URL
                    dl_url = (
                        formats.get("application/epub+zip") or
                        formats.get("application/x-mobipocket-ebook") or
                        formats.get("application/pdf") or
                        formats.get("text/plain; charset=utf-8")
                    )
                    authors = ", ".join([a.get("name", "") for a in book.get("authors", [])])
                    
                    return {
                        "title": book.get("title", "Unknown Title"),
                        "author": authors or "Unknown Author",
                        "download_url": dl_url,
                        "cover": formats.get("image/jpeg")
                    }
        except Exception as e:
            LOGGER.warning(f"[book] Gutendex search failed for '{query}': {e!r}")
        
        return {}


@run_async
def book(bot: Bot, update: Update, args: List[str]):
    msg = update.effective_message
    query = " ".join(args).strip()
    
    if not query:
        msg.reply_text(
            "Please provide a book title or title and author!\n<b>Usage:</b> <code>/book Pride and Prejudice</code>", 
            parse_mode=ParseMode.HTML
        )
        return

    status_msg = msg.reply_text(f"Searching public domain for: <b>{html.escape(query)}</b>...", parse_mode=ParseMode.HTML)
    
    book_info = BookFetcher.search_books(query)
    
    if not book_info or not book_info.get("download_url"):
        status_msg.edit_text("Sorry, no downloadable public domain book found for your query.")
        return

    title = book_info["title"]
    author = book_info["author"]
    dl_url = book_info["download_url"]
    cover_url = book_info.get("cover")

    status_msg.edit_text(
        f"Found <b>{html.escape(title)}</b> by <i>{html.escape(author)}</i>\nDownloading and sending file to you...", 
        parse_mode=ParseMode.HTML
    )

    try:
        # Download the book file
        resp = requests.get(dl_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status() 
        
        content = resp.content
        
        # Determine extension from download URL or fallback to txt
        ext = "epub" if "epub" in dl_url else ("pdf" if "pdf" in dl_url else ("mobi" if "mobi" in dl_url else "txt"))
        file_obj = io.BytesIO(content)
        
        # Sanitize the filename to prevent upload crashes
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title).replace(' ', '_')
        file_obj.name = f"{safe_title}.{ext}"

        # Attempt to fetch the cover image for the thumbnail
        thumb_obj = None
        if cover_url:
            try:
                thumb_resp = requests.get(cover_url, timeout=10)
                if thumb_resp.status_code == 200:
                    thumb_obj = io.BytesIO(thumb_resp.content)
                    thumb_obj.name = "cover.jpg"
            except Exception as thumb_err:
                LOGGER.warning(f"[book] Failed to download thumbnail for {title}: {thumb_err}")

        # Send the document with the thumbnail attached
        msg.reply_document(
            document=file_obj,
            thumb=thumb_obj,
            caption=f"<b>{html.escape(title)}</b>\nAuthor: {html.escape(author)}\n\nDownloaded via @{bot.username}",
            parse_mode=ParseMode.HTML
        )
        status_msg.delete()
        
    except Exception as e:
        LOGGER.error(f"[book] Download error: {e!r}")
        status_msg.edit_text(
            f"Failed to download file. Direct link: <a href='{dl_url}'>Download Book</a>", 
            parse_mode=ParseMode.HTML
        )


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
    def get_best_magnet(query: str) -> dict:
        """Searches TPB across all categories and returns top magnet link."""
        url = "https://apibay.org/q.php"
        try:
            # Changed cat to 0 to search absolutely everything
            resp = requests.get(url, params={"q": query, "cat": 0}, timeout=10)
            
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list) and data[0].get("id") != "0":
                    top = data[0]
                    info_hash = top.get("info_hash")
                    name = top.get("name", "Unknown_Torrent")
                    
                    magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(name)}{TRACKERS}"
                    return {"name": name, "magnet": magnet}
            else:
                # This will expose Cloudflare blocks or server outages in your logs
                LOGGER.error(f"[TPB HTTP Error] Code: {resp.status_code}, Response: {resp.text[:200]}")
                
        except Exception as e:
            LOGGER.error(f"[TPB Search Error]: {e!r}")
        return {}

@run_async
def piratebook(bot: Bot, update: Update, args: List[str]):
    msg = update.effective_message
    query = " ".join(args).strip()

    if not query:
        msg.reply_text("Please specify a book title!\n<b>Usage:</b> <code>/piratebook 1984 George Orwell</code>", parse_mode=ParseMode.HTML)
        return

    status_msg = msg.reply_text(f"Searching Pirate Bay for <b>{html.escape(query)}</b>...", parse_mode=ParseMode.HTML)

    # Step 1: Search Pirate Bay for magnet link
    result = TPBDownloader.get_best_magnet(query)
    if not result:
        status_msg.edit_text("No ebook torrents found on Pirate Bay for that query.")
        return

    torrent_name = result["name"]
    magnet_link = result["magnet"]

    status_msg.edit_text(
        f"Downloading torrent: <b>{html.escape(torrent_name)}</b>...\n<i>Please wait, this depends on active seeders.</i>", 
        parse_mode=ParseMode.HTML
    )

    # Step 2: Download torrent to temporary directory
    temp_dir = tempfile.mkdtemp()
    try:
        downloader = TorrentDownloader(magnet_link, temp_dir)
        downloader.start_download()

        # Step 3: Find downloaded .epub, .pdf, .mobi, or .azw3 file
        found_files = []
        for ext in ['*.epub', '*.pdf', '*.mobi', '*.azw3']:
            found_files.extend(glob.glob(os.path.join(temp_dir, '**', ext), recursive=True))

        if not found_files:
            # Fallback: take any file downloaded in the folder
            found_files = [f for f in glob.glob(os.path.join(temp_dir, '**', '*'), recursive=True) if os.path.isfile(f)]

        if not found_files:
            status_msg.edit_text("Download timed out or no file was extracted.")
            return

        target_file = found_files[0]
        file_size_mb = round(os.path.getsize(target_file) / (1024 * 1024), 2)

        # Telegram limit check (50MB for bots)
        if file_size_mb > 50:
            status_msg.edit_text(f"File size ({file_size_mb} MB) exceeds Telegram's 50MB bot upload limit.")
            return

        status_msg.edit_text("Uploading book to Telegram...")

        # Step 4: Upload document directly into Telegram chat
        with open(target_file, "rb") as doc:
            msg.reply_document(
                document=doc,
                caption=f"<b>{html.escape(torrent_name)}</b>\nSize: <code>{file_size_mb} MB</code>\n\nDownloaded via @{bot.username}",
                parse_mode=ParseMode.HTML
            )
        status_msg.delete()

    except Exception as e:
        LOGGER.error(f"[TPB Download Error]: {e!r}")
        status_msg.edit_text(f"Failed to download torrent file.\n<b>Error:</b> <code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)
    finally:
        # Step 5: Clean up temporary files from disk
        shutil.rmtree(temp_dir, ignore_errors=True)


# ==========================================
# MODULE REGISTRATION
# ==========================================

__help__ = """
Download ebooks directly to Telegram.

*Available commands:*
 - /book <title or author>: Searches the Gutendex API for free public domain books and sends the file directly.
 - /piratebook <title/author>: Searches Pirate Bay, downloads the ebook via torrents, and uploads it to chat.
"""

__mod_name__ = "Books"

BOOK_HANDLER = DisableAbleCommandHandler("book", book, pass_args=True)
PIRATEBOOK_HANDLER = DisableAbleCommandHandler("piratebook", piratebook, pass_args=True)

dispatcher.add_handler(BOOK_HANDLER)
dispatcher.add_handler(PIRATEBOOK_HANDLER)
