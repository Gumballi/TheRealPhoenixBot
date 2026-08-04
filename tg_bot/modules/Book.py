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
from telegram import Bot, ParseMode, Update
from telegram.ext import run_async
from torrentp import TorrentDownloader

from tg_bot import dispatcher
from tg_bot.modules.disable import DisableAbleCommandHandler

LOGGER = logging.getLogger(__name__)

# ==========================================
# OPEN LIBRARY (PUBLIC DOMAIN) LOGIC
# ==========================================

class OpenLibraryFetcher:
    @staticmethod
    def search_books(query: str) -> Optional[dict]:
        """
        Searches Open Library for books, then fetches the raw EPUB/PDF 
        directly from the Internet Archive. Immune to Cloudflare.
        """
        search_url = "https://openlibrary.org/search.json"
        
        try:
            # Step 1: Search Open Library (Standard requests, no Cloudflare block!)
            resp = requests.get(search_url, params={"q": query, "limit": 15}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            LOGGER.error(f"[OpenLibrary] Search failed: {e!r}")
            return None

        # Step 2: Look for a public domain book with an Internet Archive ID
        for doc in data.get("docs", []):
            if doc.get("public_scan_b") and doc.get("ia"):
                ia_id = doc.get("ia")[0] # Grab the Internet Archive identifier
                
                # Step 3: Check Internet Archive for the actual files
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

                    # If we found a file, package it up and return it
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
                    LOGGER.warning(f"[InternetArchive] Failed to fetch metadata for {ia_id}: {e!r}")
                    continue

        return {} # Search worked, but no downloadable files found


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
        f"Searching Open Library for: <b>{html.escape(query)}</b>...", parse_mode=ParseMode.HTML
    )

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

    status_msg.edit_text(
        f"Found <b>{html.escape(title)}</b> by <i>{html.escape(author)}</i>\nDownloading from Internet Archive...",
        parse_mode=ParseMode.HTML,
    )

    try:
        # Download from Internet Archive (No Cloudflare blocking!)
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
        except Exception:
            pass

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
# PIRATE BAY (TORRENT) LOGIC (Unchanged)
# ==========================================

TRACKERS = "&tr=" + "&tr=".join([
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
])

class TPBDownloader:
    @staticmethod
    def get_best_magnet(query: str) -> Optional[dict]:
        try:
            st_url = "https://bitsearch.to/api/v1/search"
            st_resp = requests.get(st_url, params={"q": query, "category": "all"}, timeout=15)
            st_resp.raise_for_status()
            
            st_data = st_resp.json()
            results = st_data.get("data", []) if "data" in st_data else st_data.get("results", [])
            
            if results:
                top = results[0]
                info_hash = top.get("info_hash")
                name = top.get("name") or top.get("title", "Unknown_Torrent")
                
                if info_hash:
                    magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(name)}{TRACKERS}"
                    return {"name": name, "magnet": magnet}
        except Exception as e:
            LOGGER.warning(f"[BitSearch] Search failed for '{query}': {e!r}")

        try:
            target_url = f"https://apibay.org/q.php?q={urllib.parse.quote(query)}&cat=0"
            proxy_url = f"https://api.codetabs.com/v1/proxy?quest={urllib.parse.quote(target_url)}"
            
            pb_resp = requests.get(proxy_url, timeout=20)
            pb_resp.raise_for_status()
            
            pb_data = pb_resp.json()
            if pb_data and isinstance(pb_data, list) and pb_data[0].get("id") != "0":
                top = pb_data[0]
                info_hash = top.get("info_hash")
                name = top.get("name", "Unknown_Torrent")
                
                magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(name)}{TRACKERS}"
                return {"name": name, "magnet": magnet}
                
        except Exception as e:
            safe_text = str(e).replace("<", "[").replace(">", "]")
            LOGGER.error(f"[TPB Proxy] Search failed: {safe_text}")
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
        except Exception:
            pass

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
 - /book <title or author>: Searches Open Library / Internet Archive for free books and sends the file directly.
 - /piratebook <title/author>: Searches torrent indexers, downloads the ebook, and uploads it to chat.
"""

__mod_name__ = "Books"

BOOK_HANDLER = DisableAbleCommandHandler("book", book, pass_args=True)
PIRATEBOOK_HANDLER = DisableAbleCommandHandler("piratebook", piratebook, pass_args=True)

dispatcher.add_handler(BOOK_HANDLER)
dispatcher.add_handler(PIRATEBOOK_HANDLER)
