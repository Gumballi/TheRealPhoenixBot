import html
import io
import logging
import math
import re
import shutil
import tempfile
import urllib.parse
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
# Stores search results temporarily so pagination and downloading work.
# Structure: { "search_id": { "query": str, "type": str, "results": list } }
SEARCH_CACHE = {}

def clean_cache():
    """Prevents memory leaks by clearing cache if it gets too large."""
    if len(SEARCH_CACHE) > 500:
        SEARCH_CACHE.clear()

def build_keyboard(search_id: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Builds the inline keyboard for book selection and pagination."""
    keyboard = []
    search_data = SEARCH_CACHE.get(search_id)
    if not search_data:
        return InlineKeyboardMarkup([])

    start = page * 5
    end = start + 5
    items = search_data["results"][start:end]
    
    # Book selection buttons
    for i, item in enumerate(items):
        idx = start + i
        # Truncate text so it doesn't break Telegram's button length limits
        title_trunc = item['title'][:35] + ("..." if len(item['title']) > 35 else "")
        author_trunc = item['author'][:15]
        
        btn_text = f"{i+1}. {title_trunc} | {author_trunc}"
        if item.get('ext'):
            btn_text += f" [{item['ext'].upper()}]"
            
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"b_dl|{search_id}|{idx}")])
        
    # Pagination row
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
# PROXY ROUTING NETWORK (FIREWALL BYPASS)
# ==========================================
class ProxyNetwork:
    @staticmethod
    def get(target_url: str, stream: bool = False, timeout: int = 30) -> requests.Response:
        encoded_url = urllib.parse.quote(target_url, safe='')
        proxies = [
            f"https://api.allorigins.win/raw?url={encoded_url}",
            f"https://api.codetabs.com/v1/proxy?quest={encoded_url}",
            f"https://corsproxy.io/?{encoded_url}"
        ]
        
        last_err = None
        for proxy in proxies:
            try:
                resp = requests.get(proxy, stream=stream, timeout=timeout)
                resp.raise_for_status()
                return resp
            except requests.exceptions.RequestException as e:
                last_err = e
                continue
                
        raise requests.exceptions.RequestException(f"All proxies failed. Last error: {last_err}")


# ==========================================
# OPEN LIBRARY (PUBLIC DOMAIN)
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
            LOGGER.error(f"[OpenLibrary] Search failed: {e!r}")
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
                    "cover": cover_url
                })
        return books

@run_async
def book(bot: Bot, update: Update, args: List[str]):
    msg = update.effective_message
    query = " ".join(args).strip()

    if not query:
        msg.reply_text("Please provide a book title.\n<b>Usage:</b> <code>/book Pride and Prejudice</code>", parse_mode=ParseMode.HTML)
        return

    status_msg = msg.reply_text(f"Searching public domain for: <b>{html.escape(query)}</b>...", parse_mode=ParseMode.HTML)
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
        "results": results
    }

    total_pages = math.ceil(len(results) / 5)
    kb = build_keyboard(search_id, 0, total_pages)
    
    status_msg.edit_text(
        f"Results for <b>{html.escape(query)}</b>:\nSelect a book to download:",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )


def do_openlib_download(bot: Bot, msg, item: dict):
    ia_id = item["ia_id"]
    title = item["title"]
    author = item["author"]
    cover_url = item["cover"]
    
    try:
        # Step 1: Find the actual file extension
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
            except Exception:
                pass

        msg.edit_text("Uploading file to Telegram...")
        msg.reply_document(
            document=file_obj,
            thumb=thumb_obj,
            caption=f"<b>{html.escape(title)}</b>\nAuthor: {html.escape(author)}\n\nDownloaded via @{bot.username}",
            parse_mode=ParseMode.HTML,
            timeout=120 
        )
        msg.delete()

    except Exception as e:
        LOGGER.error(f"[OpenLib DL Error] {e!r}")
        msg.edit_text("Failed to process download. The file may be unavailable.")


# ==========================================
# LIBRARY GENESIS (PROXY)
# ==========================================
class LibGenFetcher:
    @staticmethod
    def search_books(query: str) -> Optional[List[dict]]:
        req_query = urllib.parse.quote_plus(query)
        domains = ["libgen.is", "libgen.rs", "libgen.st", "libgen.li"]
        
        for domain in domains:
            try:
                url = f"https://{domain}/search.php?req={req_query}&res=25&view=simple"
                resp = ProxyNetwork.get(url, timeout=20)
                
                # Scrape rows from the HTML table
                rows = re.findall(r'<tr valign="top"[^>]*>(.*?)</tr>', resp.text, re.DOTALL | re.IGNORECASE)
                if not rows:
                    continue
                    
                books = []
                for row in rows:
                    cols = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
                    if len(cols) >= 9:
                        author = re.sub(r'<[^>]+>', '', cols[1]).strip()
                        title_html = cols[2]
                        title_match = re.search(r'md5=([a-fA-F0-9]{32})"[^>]*>(.*?)</a>', title_html, re.IGNORECASE)
                        ext = re.sub(r'<[^>]+>', '', cols[8]).strip().lower()
                        
                        if title_match:
                            md5 = title_match.group(1)
                            title = re.sub(r'<[^>]+>', '', title_match.group(2)).strip()
                            books.append({
                                "md5": md5,
                                "title": title,
                                "author": author,
                                "ext": ext
                            })
                if books:
                    return books
            except requests.exceptions.RequestException as e:
                LOGGER.warning(f"[LibGen] Proxy search failed on {domain}: {e!r}")
                continue
                
        return None 

@run_async
def piratebook(bot: Bot, update: Update, args: List[str]):
    msg = update.effective_message
    query = " ".join(args).strip()

    if not query:
        msg.reply_text("Please specify a book title.\n<b>Usage:</b> <code>/piratebook 1984 George Orwell</code>", parse_mode=ParseMode.HTML)
        return

    status_msg = msg.reply_text(f"Searching Library Genesis for: <b>{html.escape(query)}</b>...", parse_mode=ParseMode.HTML)
    results = LibGenFetcher.search_books(query)
    
    if results is None:
        status_msg.edit_text("Search failed. Proxy network could not reach Library Genesis.")
        return
    if not results:
        status_msg.edit_text("No ebooks found on Library Genesis for that query.")
        return

    clean_cache()
    search_id = uuid.uuid4().hex[:8]
    SEARCH_CACHE[search_id] = {
        "query": query,
        "type": "libgen",
        "results": results
    }

    total_pages = math.ceil(len(results) / 5)
    kb = build_keyboard(search_id, 0, total_pages)
    
    status_msg.edit_text(
        f"Results for <b>{html.escape(query)}</b>:\nSelect a book to download:",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )


def do_libgen_download(bot: Bot, msg, item: dict):
    md5 = item["md5"]
    title = item["title"]
    author = item["author"]
    ext = item["ext"]

    try:
        # Step 1: Scrape the download link from the gateway
        msg.edit_text("Fetching direct download link via proxy...")
        gate_url = f"https://library.lol/main/{md5}"
        gate_resp = ProxyNetwork.get(gate_url, timeout=20)
        
        dl_match = re.search(r'href="([^"]+)">GET</a>', gate_resp.text)
        if not dl_match:
            msg.edit_text("Failed to locate the download gateway link.")
            return
            
        dl_url = dl_match.group(1)
        
        # Step 2: Download the file
        msg.edit_text("Downloading file to server...")
        resp = ProxyNetwork.get(dl_url, stream=True, timeout=45)
        
        # Check size limits
        size = int(resp.headers.get("Content-Length", 0))
        if size > 50 * 1024 * 1024:
            msg.edit_text(
                f"File size ({round(size / 1024 / 1024, 2)} MB) exceeds Telegram's 50MB limit.\n\n"
                f"<b>Direct Download Link:</b> <a href='{html.escape(dl_url)}'>Click Here</a>", 
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

    except requests.exceptions.RequestException as e:
        LOGGER.error(f"[LibGen DL Error] {e!r}")
        msg.edit_text("Proxy download timed out. The file might be too large for the free proxy to process.")


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
        return
        
    action, search_id, param = parts
    
    if search_id not in SEARCH_CACHE:
        query.answer("This search has expired. Please run the command again.", show_alert=True)
        return
        
    search_data = SEARCH_CACHE[search_id]
    
    if action == "b_pg":
        page = int(param)
        total_pages = math.ceil(len(search_data["results"]) / 5)
        kb = build_keyboard(search_id, page, total_pages)
        
        query.edit_message_text(
            f"Results for <b>{html.escape(search_data['query'])}</b>:\nSelect a book to download:",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )
        
    elif action == "b_dl":
        idx = int(param)
        item = search_data["results"][idx]
        query.edit_message_text(f"Preparing to download: <b>{html.escape(item['title'])}</b>...", parse_mode=ParseMode.HTML)
        
        if search_data["type"] == "openlib":
            do_openlib_download(bot, query.message, item)
        elif search_data["type"] == "libgen":
            do_libgen_download(bot, query.message, item)


# ==========================================
# MODULE REGISTRATION
# ==========================================

__help__ = """
Download ebooks directly to Telegram.

*Available commands:*
 - /book <title/author>: Searches Open Library / Internet Archive for free books.
 - /piratebook <title/author>: Searches Library Genesis and uploads the direct file to chat.
"""

__mod_name__ = "Books"

BOOK_HANDLER = DisableAbleCommandHandler("book", book, pass_args=True)
PIRATEBOOK_HANDLER = DisableAbleCommandHandler("piratebook", piratebook, pass_args=True)
BOOK_BTN_HANDLER = CallbackQueryHandler(book_callback, pattern=r'^b_(pg|dl)\|')

dispatcher.add_handler(BOOK_HANDLER)
dispatcher.add_handler(PIRATEBOOK_HANDLER)
dispatcher.add_handler(BOOK_BTN_HANDLER)
