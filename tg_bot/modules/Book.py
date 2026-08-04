import html
import io
import logging
import re
import urllib.parse
from typing import List, Optional

import requests
from telegram import Bot, ParseMode, Update
from telegram.ext import run_async

from tg_bot import dispatcher
from tg_bot.modules.disable import DisableAbleCommandHandler

LOGGER = logging.getLogger(__name__)

# ==========================================
# OPEN LIBRARY (PUBLIC DOMAIN) LOGIC
# ==========================================

class OpenLibraryFetcher:
    @staticmethod
    def search_books(query: str) -> Optional[dict]:
        """Searches Open Library for public domain books via Internet Archive."""
        search_url = "https://openlibrary.org/search.json"
        
        try:
            resp = requests.get(search_url, params={"q": query, "limit": 15}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            LOGGER.error(f"[OpenLibrary] Search failed: {e!r}")
            return None

        for doc in data.get("docs", []):
            if doc.get("public_scan_b") and doc.get("ia"):
                ia_id = doc.get("ia")[0]
                ia_meta_url = f"https://archive.org/metadata/{ia_id}"
                
                try:
                    ia_resp = requests.get(ia_meta_url, timeout=10)
                    ia_resp.raise_for_status()
                    ia_data = ia_resp.json()
                    
                    files = ia_data.get("files", [])
                    dl_url = None
                    ext = "txt"

                    # Prioritize EPUB, fallback to PDF
                    for f in files:
                        if f.get("name", "").endswith(".epub"):
                            dl_url = f"https://archive.org/download/{ia_id}/{f['name']}"
                            ext = "epub"
                            break
                    
                    if not dl_url:
                        for f in files:
                            if f.get("name", "").endswith(".pdf"):
                                dl_url = f"https://archive.org/download/{ia_id}/{f['name']}"
                                ext = "pdf"
                                break

                    if dl_url:
                        authors = ", ".join(doc.get("author_name", ["Unknown Author"]))
                        cover_i = doc.get("cover_i")
                        cover_url = f"https://covers.openlibrary.org/b/id/{cover_i}-L.jpg" if cover_i else None
                        
                        return {
                            "title": doc.get("title", "Unknown Title"),
                            "author": authors,
                            "download_url": dl_url,
                            "ext": ext,
                            "cover": cover_url
                        }
                except Exception as e:
                    LOGGER.warning(f"[InternetArchive] Failed metadata for {ia_id}: {e!r}")
                    continue

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

    status_msg = msg.reply_text(f"Searching Open Library for: <b>{html.escape(query)}</b>...", parse_mode=ParseMode.HTML)
    book_info = OpenLibraryFetcher.search_books(query)

    if book_info is None:
        status_msg.edit_text("Search failed — Open Library didn't respond. Try again in a moment.")
        return
    if not book_info.get("download_url"):
        status_msg.edit_text("No downloadable public domain book found for your query.")
        return

    title = book_info["title"]
    author = book_info["author"]
    dl_url = book_info["download_url"]
    ext = book_info["ext"]
    cover_url = book_info.get("cover")

    status_msg.edit_text(f"Found <b>{html.escape(title)}</b> by <i>{html.escape(author)}</i>\nDownloading from Internet Archive...", parse_mode=ParseMode.HTML)

    try:
        resp = requests.get(dl_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=45)
        resp.raise_for_status()
        
        file_obj = io.BytesIO(resp.content)
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title).replace(" ", "_")
        file_obj.name = f"{safe_title}.{ext}"
        file_obj.seek(0)

        thumb_obj = None
        if cover_url:
            try:
                thumb_resp = requests.get(cover_url, timeout=10)
                thumb_resp.raise_for_status()
                thumb_obj = io.BytesIO(thumb_resp.content)
                thumb_obj.name = "cover.jpg"
                thumb_obj.seek(0)
            except Exception:
                pass

        # Added timeout=120 to fix the TimedOut() crash during large Telegram uploads
        msg.reply_document(
            document=file_obj,
            thumb=thumb_obj,
            caption=f"<b>{html.escape(title)}</b>\nAuthor: {html.escape(author)}\n\nDownloaded via @{bot.username}",
            parse_mode=ParseMode.HTML,
            timeout=120 
        )
        try:
            status_msg.delete()
        except Exception:
            pass

    except requests.exceptions.RequestException:
        status_msg.edit_text(f"Failed to download file. Direct link: <a href='{html.escape(dl_url)}'>Download Book</a>", parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        LOGGER.error(f"[book] Unexpected error sending document: {e!r}")
        status_msg.edit_text("Something went wrong sending the file. Check logs.")


# ==========================================
# LIBRARY GENESIS (DIRECT DOWNLOAD) LOGIC
# ==========================================

class LibGenFetcher:
    @staticmethod
    def search_books(query: str) -> Optional[dict]:
        """Scrapes LibGen and Library.lol for direct file downloads. No Torrents."""
        domains = ["libgen.is", "libgen.rs", "libgen.st"]
        
        for domain in domains:
            try:
                url = f"https://{domain}/search.php"
                resp = requests.get(url, params={"req": query, "res": 25, "view": "simple"}, timeout=15)
                resp.raise_for_status()
                
                # Extract the MD5 hash of the first result
                match = re.search(r'\?md5=([A-Fa-f0-9]{32})', resp.text, re.IGNORECASE)
                if not match:
                    continue # Try next domain if empty
                
                md5 = match.group(1)
                
                # Use the MD5 to hit the direct download gateway
                gate_url = f"https://library.lol/main/{md5}"
                gate_resp = requests.get(gate_url, timeout=15)
                gate_resp.raise_for_status()
                
                # Scrape the direct 'GET' link
                dl_match = re.search(r'href="([^"]+)">GET</a>', gate_resp.text)
                if not dl_match:
                    return {}
                    
                dl_url = dl_match.group(1)
                
                # Scrape Title, Author, and Extension from the gateway page
                title_match = re.search(r'<h1>(.*?)</h1>', gate_resp.text, re.IGNORECASE | re.DOTALL)
                title = title_match.group(1).strip() if title_match else query
                
                author_match = re.search(r'Author\(s\):\s*(.*?)(?:<br|</div>)', gate_resp.text, re.IGNORECASE)
                author = author_match.group(1).strip() if author_match else "Unknown Author"
                
                ext_match = re.search(r'Extension:\s*([a-zA-Z0-9]+)', gate_resp.text, re.IGNORECASE)
                ext = ext_match.group(1).lower() if ext_match else "epub"
                ext = re.sub(r'[^a-z0-9]', '', ext) # Sanitize extension
                
                return {
                    "title": title,
                    "author": author,
                    "download_url": dl_url,
                    "ext": ext
                }
            except requests.exceptions.RequestException as e:
                LOGGER.warning(f"[LibGen] Failed on {domain}: {e!r}")
                continue
                
        return None # All domains completely failed


@run_async
def piratebook(bot: Bot, update: Update, args: List[str]):
    msg = update.effective_message
    query = " ".join(args).strip()

    if not query:
        msg.reply_text("Please specify a book title!\n<b>Usage:</b> <code>/piratebook 1984 George Orwell</code>", parse_mode=ParseMode.HTML)
        return

    status_msg = msg.reply_text(f"Searching Library Genesis for: <b>{html.escape(query)}</b>...", parse_mode=ParseMode.HTML)

    book_info = LibGenFetcher.search_books(query)
    
    if book_info is None:
        status_msg.edit_text("Search failed — LibGen is currently unresponsive.")
        return
    if not book_info.get("download_url"):
        status_msg.edit_text("No ebooks found on Library Genesis for that query.")
        return

    title = book_info["title"]
    author = book_info["author"]
    dl_url = book_info["download_url"]
    ext = book_info["ext"]

    status_msg.edit_text(f"Found <b>{html.escape(title)}</b> by <i>{html.escape(author)}</i>\nDownloading file directly to server...", parse_mode=ParseMode.HTML)

    try:
        # Stream the download so we can check the file size before loading it all into memory
        resp = requests.get(dl_url, headers={"User-Agent": "Mozilla/5.0"}, stream=True, timeout=20)
        resp.raise_for_status()
        
        size = int(resp.headers.get("Content-Length", 0))
        if size > 50 * 1024 * 1024:
            status_msg.edit_text(f"⚠️ File size ({round(size / 1024 / 1024, 2)} MB) exceeds Telegram's 50MB bot limit.\n\nDirect link: <a href='{html.escape(dl_url)}'>Download Here</a>", parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return

        # Read into memory
        content = resp.content
        file_obj = io.BytesIO(content)
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title).replace(" ", "_")
        file_obj.name = f"{safe_title}.{ext}"
        file_obj.seek(0)

        status_msg.edit_text("Uploading book to Telegram...")

        # Added timeout=120 here as well to prevent Telegram API disconnects
        msg.reply_document(
            document=file_obj,
            caption=f"<b>{html.escape(title)}</b>\nAuthor: {html.escape(author)}\n\nDownloaded via @{bot.username}",
            parse_mode=ParseMode.HTML,
            timeout=120
        )
            
        try:
            status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        LOGGER.error(f"[LibGen Download Error]: {e!r}")
        status_msg.edit_text(f"Failed to download file.\nDirect link: <a href='{html.escape(dl_url)}'>Download Book</a>", parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ==========================================
# MODULE REGISTRATION
# ==========================================

__help__ = """
Download ebooks directly to Telegram.

*Available commands:*
 - /book <title or author>: Searches Open Library / Internet Archive for free books and sends the file directly.
 - /piratebook <title/author>: Searches Library Genesis, bypasses torrents, and uploads the direct file to chat.
"""

__mod_name__ = "Books"

BOOK_HANDLER = DisableAbleCommandHandler("book", book, pass_args=True)
PIRATEBOOK_HANDLER = DisableAbleCommandHandler("piratebook", piratebook, pass_args=True)

dispatcher.add_handler(BOOK_HANDLER)
dispatcher.add_handler(PIRATEBOOK_HANDLER)
