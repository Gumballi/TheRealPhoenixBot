import html
import io
import logging
import math
import re
import time
import uuid
from typing import List, Optional

import requests
from telegram import Bot, ParseMode, Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import run_async, CallbackQueryHandler

from tg_bot import dispatcher
from tg_bot.modules.disable import DisableAbleCommandHandler

LOGGER = logging.getLogger(__name__)

# ==========================================
# STATE MANAGEMENT (CACHE)
# ==========================================
SEARCH_CACHE = {}
CACHE_TTL_SECONDS = 30 * 60  # 30 minutes
CACHE_MAX_ENTRIES = 500

def clean_cache():
    """Evict expired entries individually instead of wiping everyone's session at once."""
    now = time.time()
    expired = [sid for sid, data in SEARCH_CACHE.items() if now - data["created"] > CACHE_TTL_SECONDS]
    for sid in expired:
        SEARCH_CACHE.pop(sid, None)

    if len(SEARCH_CACHE) > CACHE_MAX_ENTRIES:
        by_age = sorted(SEARCH_CACHE.items(), key=lambda kv: kv[1]["created"])
        overflow = len(SEARCH_CACHE) - CACHE_MAX_ENTRIES
        for sid, _ in by_age[:overflow]:
            SEARCH_CACHE.pop(sid, None)

def build_keyboard(search_id: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    keyboard = []
    search_data = SEARCH_CACHE.get(search_id)
    if not search_data:
        return InlineKeyboardMarkup([])

    start = page * 5
    end = start + 5
    items = search_data["results"][start:end]

    for i, item in enumerate(items):
        idx = start + i
        title_trunc = item["title"][:32] + ("..." if len(item["title"]) > 32 else "")
        author_trunc = item["author"][:15]

        btn_text = f"{i+1}. {title_trunc} | {author_trunc}"
        if item.get('ext'):
            btn_text += f" [{item['ext'].upper()}]"
            
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"b_dl|{search_id}|{idx}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("< Prev", callback_data=f"b_pg|{search_id}|{page-1}"))

    nav_row.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="ignore"))

    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next >", callback_data=f"b_pg|{search_id}|{page+1}"))

    if nav_row:
        keyboard.append(nav_row)

    return InlineKeyboardMarkup(keyboard)


# ==========================================
# OPEN LIBRARY (PUBLIC DOMAIN - /book)
# ==========================================
class OpenLibraryFetcher:
    @staticmethod
    def search_books(query: str) -> Optional[List[dict]]:
        search_url = "https://openlibrary.org/search.json"
        try:
            resp = requests.get(search_url, params={"q": query, "limit": 30}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            safe_err = str(e).replace("<", "[").replace(">", "]")
            LOGGER.error(f"[OpenLibrary] Search failed: {safe_err}")
            return None
        except ValueError as e:
            safe_err = str(e).replace("<", "[").replace(">", "]")
            LOGGER.error(f"[OpenLibrary] Non-JSON response: {safe_err}")
            return None

        books = []
        for doc in data.get("docs", []):
            if doc.get("public_scan_b") and doc.get("ia"):
                ia_id = doc.get("ia")[0]
                title = doc.get("title", "Unknown Title")
                authors = ", ".join(doc.get("author_name", ["Unknown Author"]))
                cover_i = doc.get("cover_i")
                cover_url = f"https://covers.openlibrary.org/b/id/{cover_i}-L.jpg" if cover_i else None

                books.append({
                    "ia_id": ia_id,
                    "title": title,
                    "author": authors,
                    "cover": cover_url,
                })
        return books


@run_async
def book(bot: Bot, update: Update, args: List[str]):
    msg = update.effective_message
    query = " ".join(args).strip()

    if not query:
        msg.reply_text(
            "Please provide a book title.\n<b>Usage:</b> <code>/book Pride and Prejudice</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    status_msg = msg.reply_text(
        f"Searching public domain for: <b>{html.escape(query)}</b>...", parse_mode=ParseMode.HTML
    )
    results = OpenLibraryFetcher.search_books(query)

    if results is None:
        status_msg.edit_text("Search failed. Open Library didn't respond. Try again later.")
        return
    if not results:
        status_msg.edit_text("No public domain books found for that query.")
        return

    clean_cache()
    search_id = uuid.uuid4().hex[:8]
    SEARCH_CACHE[search_id] = {
        "query": query,
        "type": "openlib",
        "results": results,
        "created": time.time(),
    }

    total_pages = math.ceil(len(results) / 5)
    kb = build_keyboard(search_id, 0, total_pages)

    status_msg.edit_text(
        f"Results for <b>{html.escape(query)}</b>:\nSelect a book to download:",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


def do_openlib_download(bot: Bot, msg, item: dict):
    ia_id = item["ia_id"]
    title = item["title"]
    author = item["author"]
    cover_url = item["cover"]

    try:
        ia_meta_url = f"https://archive.org/metadata/{ia_id}"
        ia_resp = requests.get(ia_meta_url, timeout=10)
        ia_resp.raise_for_status()
        files = ia_resp.json().get("files", [])

        dl_url = None
        ext = "txt"
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

        if not dl_url:
            msg.edit_text("Could not find a downloadable EPUB or PDF for this specific edition.")
            return

        msg.edit_text("Downloading from Internet Archive...")
        resp = requests.get(dl_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
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
                LOGGER.warning(f"[OpenLib] Cover fetch failed for {ia_id}: {thumb_err!r}")

        msg.edit_text("Uploading file to Telegram...")
        msg.reply_document(
            document=file_obj,
            thumb=thumb_obj,
            caption=f"<b>{html.escape(title)}</b>\nAuthor: {html.escape(author)}\n\nDownloaded via @{bot.username}",
            parse_mode=ParseMode.HTML,
            timeout=120,
        )

        try:
            msg.delete()
        except Exception as del_err:
            LOGGER.warning(f"[OpenLib] Could not delete status message: {del_err!r}")

    except Exception as e:
        safe_err = str(e).replace("<", "[").replace(">", "]")
        LOGGER.error(f"[OpenLib DL Error] {safe_err}")
        msg.edit_text("Failed to process download. The file may be unavailable.")


# ==========================================
# ANNA'S ARCHIVE (DIRECT HTML PARSER - /piratebook)
# ==========================================
class PirateBookFetcher:
    @staticmethod
    def search_books(query: str) -> Optional[List[dict]]:
        domains = ["annas-archive.gl", "annas-archive.pk", "annas-archive.gd", "annas-archive.se"]
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        
        for domain in domains:
            search_url = f"https://{domain}/search"
            try:
                resp = requests.get(search_url, params={"q": query}, headers=headers, timeout=15)
                if resp.status_code != 200:
                    continue
                    
                books = []
                seen_md5s = set()
                
                # Scan for MD5/slow_download links and extract surrounding structured context block
                for match in re.finditer(r'href="/(?:md5|slow_download)/([a-fA-F0-9]{32})"', resp.text):
                    md5 = match.group(1)
                    if md5 in seen_md5s:
                        continue
                    seen_md5s.add(md5)
                    
                    start = max(0, match.start() - 300)
                    end = min(len(resp.text), match.end() + 900)
                    block = resp.text[start:end]
                    
                    # Extract Title cleanly from main link text
                    title = "Unknown Title"
                    title_match = re.search(r'href="/(?:md5|slow_download)/[a-fA-F0-9]{32}"[^>]*>(.*?)</a>', block, re.DOTALL)
                    if title_match:
                        raw_t = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
                        if raw_t and len(raw_t) > 1 and raw_t.lower() not in ["download", "slow download", "fast download"]:
                            title = raw_t
                            
                    # Extract Author cleanly from secondary metadata blocks
                    author = "Unknown Author"
                    author_match = re.search(r'<div[^>]*class="[^"]*text-gray-[^"]*"[^>]*>(.*?)</div>', block, re.DOTALL)
                    if not author_match:
                        author_match = re.search(r'<div[^>]*class="[^"]*italic[^"]*"[^>]*>(.*?)</div>', block, re.DOTALL)
                        
                    if author_match:
                        raw_a = re.sub(r'<[^>]+>', '', author_match.group(1)).strip()
                        if raw_a and len(raw_a) < 80 and not any(unit in raw_a for unit in ["MB", "GB", "PDF", "EPUB"]):
                            author = raw_a

                    # Extension detector
                    ext = "epub"
                    if "pdf" in block.lower():
                        ext = "pdf"
                    elif "mobi" in block.lower():
                        ext = "mobi"

                    books.append({
                        "md5": md5,
                        "title": title[:100],
                        "author": author[:80],
                        "ext": ext
                    })
                    if len(books) >= 25:
                        break
                        
                if books:
                    return books
            except Exception as e:
                safe_err = str(e).replace("<", "[").replace(">", "]")
                LOGGER.warning(f"[PirateBook] Search failed on {domain}: {safe_err}")
                continue
                
        return None


@run_async
def piratebook(bot: Bot, update: Update, args: List[str]):
    msg = update.effective_message
    query = " ".join(args).strip()

    if not query:
        msg.reply_text(
            "Please specify a book title or author!\n<b>Usage:</b> <code>/piratebook Franz Kafka</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    status_msg = msg.reply_text(
        f"Searching archives for: <b>{html.escape(query)}</b>...", parse_mode=ParseMode.HTML
    )
    results = PirateBookFetcher.search_books(query)

    if results is None:
        status_msg.edit_text("Search failed. Could not reach active archive networks.")
        return
    if not results:
        status_msg.edit_text("No ebooks found for that query.")
        return

    clean_cache()
    search_id = uuid.uuid4().hex[:8]
    SEARCH_CACHE[search_id] = {
        "query": query,
        "results": results,
        "type": "pirate",
        "created": time.time(),
    }

    total_pages = math.ceil(len(results) / 5)
    kb = build_keyboard(search_id, 0, total_pages)

    status_msg.edit_text(
        f"Results for <b>{html.escape(query)}</b>:\nSelect a book to download:",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


def do_pirate_download(bot: Bot, msg, item: dict):
    md5 = item["md5"]
    title = item["title"]
    author = item["author"]
    ext = item["ext"]

    try:
        msg.edit_text("Fetching download link from archive...")
        dl_url = f"https://annas-archive.gl/slow_download/{md5}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        
        page_resp = None
        for attempt in range(3):
            page_resp = requests.get(dl_url, headers=headers, timeout=20)
            if "Flood control exceeded" in page_resp.text or page_resp.status_code == 429:
                if attempt < 2:
                    time.sleep(4)
                    continue
            break
            
        if page_resp is None or "Flood control exceeded" in page_resp.text:
            raise Exception("Flood control exceeded. Please try again in a few moments.")
        
        file_link_match = re.search(r'href="(https?://[^"]+)"[^>]*>Download pilihan|href="(https?://[^"]+\.(epub|pdf|mobi)[^"]*)"', page_resp.text, re.IGNORECASE)
        if not file_link_match:
            file_link_match = re.search(r'href="(https?://(gen\.lib\.rus\.ec|library\.lol|dl\.booksdl\.org|cloudflare-ipfs\.com)[^"]+)"', page_resp.text)
            
        if file_link_match:
            actual_dl = [g for g in file_link_match.groups() if g and g.startswith('http')][0]
        else:
            actual_dl = f"https://annas-archive.gl/md5/{md5}"

        msg.edit_text("Downloading file to server...")
        resp = requests.get(actual_dl, headers=headers, stream=True, timeout=45)
        
        size = int(resp.headers.get("Content-Length", 0))
        if size > 50 * 1024 * 1024:
            msg.edit_text(
                f"File size exceeds Telegram's 50MB limit.\n\n"
                f"<b>Direct Download Link:</b> <a href='{html.escape(actual_dl)}'>Click Here</a>", 
                parse_mode=ParseMode.HTML, 
                disable_web_page_preview=True
            )
            return

        content = resp.content
        file_obj = io.BytesIO(content)
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title).replace(" ", "_")
        file_obj.name = f"{safe_title}.{ext}"
        file_obj.seek(0)

        msg.edit_text("Uploading book to Telegram...")
        msg.reply_document(
            document=file_obj,
            caption=f"<b>{html.escape(title)}</b>\nAuthor: {html.escape(author)}\n\nDownloaded via @{bot.username}",
            parse_mode=ParseMode.HTML,
            timeout=120
        )
        msg.delete()

    except Exception as e:
        safe_err = str(e).replace("<", "[").replace(">", "]")
        LOGGER.error(f"[Pirate DL Error] {safe_err}")
        msg.edit_text(
            f"Direct download stream failed due to rate limiting.\n\n<b>Mirror Link:</b> <a href='https://annas-archive.gl/md5/{md5}'>Open on Anna's Archive</a>", 
            parse_mode=ParseMode.HTML, 
            disable_web_page_preview=True
        )


# ==========================================
# CALLBACK HANDLER
# ==========================================
@run_async
def book_callback(bot: Bot, update: Update):
    query = update.callback_query
    data = query.data

    if data == "ignore":
        query.answer()
        return

    parts = data.split("|")
    if len(parts) != 3:
        query.answer()
        return

    action, search_id, param = parts

    if search_id not in SEARCH_CACHE:
        query.answer("This search has expired. Please run the command again.", show_alert=True)
        return

    search_data = SEARCH_CACHE[search_id]
    search_data["created"] = time.time()  # sliding expiration refresh

    if action == "b_pg":
        query.answer()
        page = int(param)
        total_pages = math.ceil(len(search_data["results"]) / 5)
        kb = build_keyboard(search_id, page, total_pages)

        query.edit_message_text(
            f"Results for <b>{html.escape(search_data['query'])}</b>:\nSelect a book to download:",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )

    elif action == "b_dl":
        idx = int(param)
        results = search_data["results"]
        if idx < 0 or idx >= len(results):
            query.answer("That selection is no longer valid. Please search again.", show_alert=True)
            return

        query.answer()
        item = results[idx]
        query.edit_message_text(
            f"Preparing to download: <b>{html.escape(item['title'])}</b>...", parse_mode=ParseMode.HTML
        )
        
        if search_data["type"] == "openlib":
            do_openlib_download(bot, query.message, item)
        elif search_data["type"] == "pirate":
            do_pirate_download(bot, query.message, item)


# ==========================================
# MODULE REGISTRATION
# ==========================================

__help__ = """
Download ebooks directly to Telegram.

*Available commands:*
 - /book <title or author>: Searches Open Library public domain scans with interactive selection.
 - /piratebook <title or author>: Searches Anna's Archive with direct HTML block parsing for titles and authors.
"""

__mod_name__ = "Books"

BOOK_HANDLER = DisableAbleCommandHandler("book", book, parse_args=True)
PIRATEBOOK_HANDLER = DisableAbleCommandHandler("piratebook", piratebook, parse_args=True)
BOOK_BTN_HANDLER = CallbackQueryHandler(book_callback, pattern=r'^b_(pg|dl)\|')

dispatcher.add_handler(BOOK_HANDLER)
dispatcher.add_handler(PIRATEBOOK_HANDLER)
dispatcher.add_handler(BOOK_BTN_HANDLER)
