import os
import re
import time
import io
import json
import logging
import math
import html
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram.ext import CallbackQueryHandler, run_async

# Import PhoenixBot specific modules
from tg_bot import dispatcher
from tg_bot.modules.disable import DisableAbleCommandHandler

# Configure logging
LOGGER = logging.getLogger(__name__)

# --- Constants & Configuration ---
# Updated list of currently active Anna's Archive domains
ANNAS_DOMAINS = ["annas-archive.gs", "annas-archive.li", "annas-archive.se", "annas-archive.org", "annas-archive.gl"]
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

# Reduced retries to 1. If a domain has a DNS error, we want it to fail fast and move to the next.
RETRY_STRATEGY = Retry(
    total=1,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
    raise_on_status=False
)

session = requests.Session()
adapter = HTTPAdapter(max_retries=RETRY_STRATEGY, pool_connections=100, pool_maxsize=100)
session.mount("http://", adapter)
session.mount("https://", adapter)

# --- Global Caches (PhoenixBot Style) ---
OPENLIB_RESULTS = {}
ABOOK_RESULTS = {}
CACHE_TIMESTAMPS = {}

def cleanup_caches():
    """Prevents memory leaks by deleting searches older than 30 minutes."""
    current_time = time.time()
    expired_keys = [msg_id for msg_id, ts in CACHE_TIMESTAMPS.items() if current_time - ts > 1800]
    for msg_id in expired_keys:
        OPENLIB_RESULTS.pop(msg_id, None)
        ABOOK_RESULTS.pop(msg_id, None)
        CACHE_TIMESTAMPS.pop(msg_id, None)


# ==========================================
# DATA CLASSES
# ==========================================
@dataclass
class BookFile:
    extension: str
    size: int = 0
    url: Optional[str] = None
    md5: Optional[str] = None
    quality: str = "standard"
    
    @property
    def size_mb(self) -> float:
        return self.size / (1024 * 1024)
    
    @property
    def is_valid(self) -> bool:
        return True

@dataclass
class Book:
    md5: str
    title: str
    author: str
    year: str
    domain: str = ""  # Remembers the working domain to prevent DNS errors on download
    publisher: str = ""
    language: str = ""
    pages: int = 0
    files: List[BookFile] = field(default_factory=list)
    cover_url: Optional[str] = None
    description: Optional[str] = None
    isbns: List[str] = field(default_factory=list)
    
    def get_available_formats(self) -> List[str]:
        return sorted(set(f.extension.lower() for f in self.files))


# ==========================================
# 1. OPEN LIBRARY ENGINE (/book)
# ==========================================
class OpenLibraryFetcher:
    @staticmethod
    def search(query: str) -> Optional[List[dict]]:
        search_url = "https://openlibrary.org/search.json"
        try:
            resp = session.get(search_url, params={"q": query, "limit": 30}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            
            books = []
            for doc in data.get("docs", []):
                if doc.get("public_scan_b") and doc.get("ia"):
                    books.append({
                        "ia_id": doc.get("ia")[0],
                        "title": doc.get("title", "Unknown Title"),
                        "author": ", ".join(doc.get("author_name", ["Unknown Author"]))
                    })
            return books
        except Exception as e:
            LOGGER.error(f"[OpenLibrary] Search failed: {e}")
            return None

def build_openlib_keyboard(msg_id: int, page: int, total_pages: int, results: list) -> InlineKeyboardMarkup:
    keyboard = []
    start = page * 5
    end = start + 5
    
    for i, item in enumerate(results[start:end]):
        idx = start + i
        title_trunc = item["title"][:32] + ("..." if len(item["title"]) > 32 else "")
        author_trunc = item["author"][:15]
        btn_text = f"{i+1}. {title_trunc} | {author_trunc}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"ol_dl_{msg_id}_{idx}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"ol_pg_{msg_id}_{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="ol_ignore"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"ol_pg_{msg_id}_{page+1}"))
        
    if nav_row:
        keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=f"ol_cancel_{msg_id}")])
    
    return InlineKeyboardMarkup(keyboard)

@run_async
def book_command(bot, update: Update, args):
    """Search Open Library for public domain books."""
    msg = update.effective_message
    if not args:
        msg.reply_text(
            "📚 *Open Library Search*\n\n"
            "Search for public domain books and classics.\n"
            "Example: `/book Pride and Prejudice`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
        
    cleanup_caches()
    query = ' '.join(args).strip()
    status_msg = msg.reply_text(f"🔍 Searching Open Library for '{query}'...")
    
    results = OpenLibraryFetcher.search(query)
    if results is None:
        status_msg.edit_text("❌ Search failed. Open Library didn't respond.")
        return
    if not results:
        status_msg.edit_text("😕 No public domain books found for that query.")
        return
        
    OPENLIB_RESULTS[status_msg.message_id] = {"query": query, "results": results}
    CACHE_TIMESTAMPS[status_msg.message_id] = time.time()
    
    total_pages = math.ceil(len(results) / 5)
    kb = build_openlib_keyboard(status_msg.message_id, 0, total_pages, results)
    
    status_msg.edit_text(
        f"📚 *Results for '{query}'*\nSelect a book to download:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN
    )

@run_async
def openlib_callback(bot, update: Update):
    """Handle Open Library inline buttons."""
    query = update.callback_query
    data = query.data
    
    if data == "ol_ignore":
        query.answer()
        return
        
    parts = data.split("_")
    action, msg_id = parts[1], int(parts[2])
    
    if data.startswith("ol_cancel"):
        query.edit_message_text("❌ Search cancelled.")
        query.answer()
        return
        
    search_data = OPENLIB_RESULTS.get(msg_id)
    if not search_data:
        query.answer("Search expired. Please run /book again.", show_alert=True)
        return
        
    CACHE_TIMESTAMPS[msg_id] = time.time()
    results = search_data["results"]
    
    if action == "pg":
        page = int(parts[3])
        total_pages = math.ceil(len(results) / 5)
        kb = build_openlib_keyboard(msg_id, page, total_pages, results)
        query.edit_message_text(
            f"📚 *Results for '{search_data['query']}'*\nSelect a book to download:",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN
        )
        
    elif action == "dl":
        idx = int(parts[3])
        item = results[idx]
        query.answer("Fetching metadata...")
        
        status_msg = query.message.edit_text(f"⬇️ Preparing '{item['title']}' from Internet Archive...")
        
        try:
            ia_meta = session.get(f"https://archive.org/metadata/{item['ia_id']}", timeout=10).json()
            files = ia_meta.get("files", [])
            
            dl_url, ext = None, "txt"
            for f in files:
                if f.get("name", "").endswith(".epub"):
                    dl_url, ext = f"https://archive.org/download/{item['ia_id']}/{f['name']}", "epub"
                    break
            if not dl_url:
                for f in files:
                    if f.get("name", "").endswith(".pdf"):
                        dl_url, ext = f"https://archive.org/download/{item['ia_id']}/{f['name']}", "pdf"
                        break
                        
            if not dl_url:
                status_msg.edit_text("❌ No EPUB or PDF available for this edition.")
                return
                
            status_msg.edit_text("Downloading file to server...")
            resp = session.get(dl_url, stream=True, timeout=45)
            
            file_obj = io.BytesIO(resp.content)
            safe_title = re.sub(r'[\\/*?:"<>|]', "", item["title"]).replace(" ", "_")
            file_obj.name = f"{safe_title}.{ext}"
            file_obj.seek(0)
            
            status_msg.edit_text("Uploading to Telegram...")
            bot.send_document(
                chat_id=query.message.chat_id,
                document=file_obj,
                filename=file_obj.name,
                caption=f"📚 *{item['title']}*\n✍️ *Author:* {item['author']}\n\n_Downloaded via Open Library_",
                parse_mode=ParseMode.MARKDOWN,
                timeout=120
            )
            status_msg.delete()
            
        except Exception as e:
            LOGGER.error(f"[OpenLib DL Error] {e}")
            status_msg.edit_text("❌ Download failed. The file may be unavailable.")


# ==========================================
# 2. ANNA'S ARCHIVE ENGINE (/abook)
# ==========================================
def search_annas_archive(query: str, format_filter: Optional[str] = None) -> List[Book]:
    """Search Anna's Archive with highly robust HTML block parsing."""
    params = {"q": query}
    if format_filter:
        params["ext"] = format_filter.lower()
        
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    for domain in ANNAS_DOMAINS:
        try:
            resp = session.get(f"https://{domain}/search", params=params, headers=headers, timeout=12)
            if resp.status_code == 200:
                books = []
                seen_md5s = set()
                
                for match in re.finditer(r'href="/(?:md5|slow_download)/([a-fA-F0-9]{32})"', resp.text):
                    md5 = match.group(1)
                    if md5 in seen_md5s:
                        continue
                    seen_md5s.add(md5)
                    
                    start = max(0, match.start() - 50)
                    end = min(len(resp.text), match.end() + 1000)
                    block = resp.text[start:end]
                    
                    # --- Robust Title/Author Extraction ---
                    title = "Unknown Title"
                    author = "Unknown Author"
                    
                    # Target new structural classes
                    t_match = re.search(r'<(?:h3|div)[^>]*class="[^"]*(?:text-xl|font-bold|text-lg)[^"]*"[^>]*>(.*?)</(?:h3|div)>', block, re.IGNORECASE | re.DOTALL)
                    if t_match:
                        title = html.unescape(re.sub(r'<[^>]+>', '', t_match.group(1)).strip())
                        
                    a_match = re.search(r'<div[^>]*class="[^"]*(?:italic|text-sm text-gray-500|author)[^"]*"[^>]*>(.*?)</div>', block, re.IGNORECASE | re.DOTALL)
                    if a_match:
                        raw_a = html.unescape(re.sub(r'<[^>]+>', '', a_match.group(1)).strip())
                        if not any(unit in raw_a.upper() for unit in ["MB", "GB", "PDF", "EPUB"]):
                            author = raw_a

                    # Fallback generic tag stripping if classes fail
                    if title == "Unknown Title":
                        raw_text = html.unescape(re.sub(r'<[^>]+>', '\n', block))
                        lines = [line.strip() for line in raw_text.split('\n') if line.strip() and not re.match(r'^(download|slow download|md5)', line, re.IGNORECASE)]
                        if len(lines) > 0:
                            title = lines[0]
                        if len(lines) > 1 and "Unknown" in author:
                            if not any(unit in lines[1].upper() for unit in ["MB", "GB", "PDF", "EPUB"]):
                                author = lines[1]

                    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', block)
                    year = year_match.group(1) if year_match else ""

                    formats = set(f.lower() for f in re.findall(r'\b(pdf|epub|mobi|azw3|djvu)\b', block, re.IGNORECASE))
                    if not formats:
                        formats = {"epub"}

                    files = [BookFile(extension=fmt) for fmt in formats]
                    
                    # Store the specific 'domain' that succeeded to prevent DNS loops during download
                    books.append(Book(md5=md5, title=title[:100], author=author[:80], year=year, files=files, domain=domain))
                    
                    if len(books) >= 20:
                        break
                if books:
                    return books
        except Exception:
            continue
    raise Exception("Could not reach active archive networks.")

def get_abook_download_url(md5: str, known_working_domain: str) -> Optional[str]:
    """Uses domain memory to instantly query the known active mirror."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    # Put the domain that successfully executed the search at the front of the line
    domains_to_try = [known_working_domain] + [d for d in ANNAS_DOMAINS if d != known_working_domain]
    
    for domain in domains_to_try:
        try:
            resp = session.get(f"https://{domain}/slow_download/{md5}", headers=headers, timeout=12)
            if resp.status_code == 200:
                all_links = re.findall(r'href="(https?://[^"]+)"', resp.text)
                for link in all_links:
                    if any(ext in link.lower() for ext in ['.epub', '.pdf', '.mobi']) and 'annas-archive' not in link.lower():
                        return link
                for link in all_links:
                    if any(kw in link.lower() for kw in ['library.lol', 'libgen', 'ipfs']) and 'annas-archive' not in link.lower():
                        return link
                return f"https://{domain}/md5/{md5}"
        except Exception:
            continue
    return None

@run_async
def abook_command(bot, update: Update, args):
    """Search for books on Anna's Archive."""
    msg = update.effective_message
    if not args:
        msg.reply_text(
            "📚 *Anna's Archive Search*\n\n"
            "Search and download any book directly.\n"
            "Example: `/abook Franz Kafka`\n\n"
            "Filter by format:\n"
            "`/abook Franz Kafka --format epub`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    cleanup_caches()
    query = ' '.join(args)
    format_filter = None
    format_match = re.search(r'--format\s+(\w+)', query, re.IGNORECASE)
    if format_match:
        format_filter = format_match.group(1)
        query = re.sub(r'--format\s+\w+', '', query).strip()
    
    status_msg = msg.reply_text(f"🔍 Searching Anna's Archive for '{query}'...")
    
    try:
        books = search_annas_archive(query, format_filter)
        if not books:
            status_msg.edit_text("😕 No results found. Try different keywords.")
            return
        
        ABOOK_RESULTS[status_msg.message_id] = books
        CACHE_TIMESTAMPS[status_msg.message_id] = time.time()
        
        keyboard = []
        for i, book in enumerate(books[:10]):
            formats = book.get_available_formats()
            format_str = ', '.join(f.upper() for f in formats[:2])
            button_text = f"{i+1}. {book.title} | {book.author} [{format_str}]"
            if len(button_text) > 60:
                button_text = button_text[:57] + "..."
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"ab_opt_{status_msg.message_id}_{i}")])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="ab_cancel")])
        
        status_msg.edit_text(
            f"📚 Found {len(books)} books. Select one to download:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        LOGGER.error(f"ABook Search error: {e}")
        status_msg.edit_text(f"❌ Error: {str(e)}")

@run_async
def abook_menu_callback(bot, update: Update):
    """Handle Anna's Archive book selection menus."""
    query = update.callback_query
    
    if query.data == "ab_cancel":
        query.edit_message_text("❌ Search cancelled.")
        query.answer()
        return

    # Handle Back button
    if query.data.startswith("ab_back_"):
        _, msg_id_str = query.data.split("_", 2)[1:]
        msg_id = int(msg_id_str)
        books = ABOOK_RESULTS.get(msg_id)
        if not books:
            query.answer("Search expired.", show_alert=True)
            return
            
        keyboard = []
        for i, book in enumerate(books[:10]):
            formats = book.get_available_formats()
            format_str = ', '.join(f.upper() for f in formats[:2])
            button_text = f"{i+1}. {book.title} | {book.author} [{format_str}]"
            if len(button_text) > 60:
                button_text = button_text[:57] + "..."
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"ab_opt_{msg_id}_{i}")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="ab_cancel")])
        
        query.edit_message_text(
            f"📚 Found {len(books)} books. Select one to download:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Handle Format Selection Menu
    if query.data.startswith("ab_opt_"):
        _, _, msg_id_str, idx_str = query.data.split("_", 3)
        msg_id, idx = int(msg_id_str), int(idx_str)
        
        books = ABOOK_RESULTS.get(msg_id)
        if not books or idx >= len(books):
            query.answer("Search has expired. Please run /abook again.", show_alert=True)
            return
        
        book = books[idx]
        formats = book.get_available_formats()
        
        keyboard = []
        row_btns = []
        for fmt_idx, fmt in enumerate(formats):
            btn = InlineKeyboardButton(f"📄 {fmt.upper()}", callback_data=f"ab_dl_{msg_id}_{idx}_{fmt_idx}")
            row_btns.append(btn)
            if len(row_btns) == 2:
                keyboard.append(row_btns)
                row_btns = []
        if row_btns:
            keyboard.append(row_btns)
        
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"ab_back_{msg_id}")])
        
        query.edit_message_text(
            f"*{book.title}*\n\n"
            f"✍️ *Author:* {book.author}\n"
            f"📅 *Year:* {book.year or 'N/A'}\n\n"
            f"Select format to download:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

@run_async
def abook_dl_callback(bot, update: Update):
    """Handle actual download execution for Anna's Archive."""
    query = update.callback_query
    _, _, msg_id_str, idx_str, fmt_idx_str = query.data.split("_", 4)
    msg_id, idx, fmt_idx = int(msg_id_str), int(idx_str), int(fmt_idx_str)
    
    books = ABOOK_RESULTS.get(msg_id)
    if not books or idx >= len(books):
        query.answer("Search expired.", show_alert=True)
        return
    
    book = books[idx]
    formats = book.get_available_formats()
    selected_format = formats[fmt_idx]
    
    query.answer(f"Downloading {selected_format.upper()}...")
    status_msg = query.message.edit_text(f"⬇️ Downloading '{book.title}' [{selected_format.upper()}] to server...")
    
    try:
        # Utilizing domain memory to prevent DNS lookup errors
        download_url = get_abook_download_url(book.md5, book.domain)
        
        if not download_url:
            status_msg.edit_text("❌ Failed to extract direct download link from archive.")
            return
            
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = session.get(download_url, headers=headers, stream=True, timeout=45)
        content = resp.content

        # Guard against HTML captchas
        if content.startswith(b'<!DOCTYPE') or b'captcha' in content[:500].lower():
            status_msg.edit_text(f"❌ Mirror blocked request.\n\n[Open in Browser](https://{book.domain}/md5/{book.md5})", parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
            return

        size = len(content)
        if size > MAX_FILE_SIZE:
            status_msg.edit_text(f"⚠️ File exceeds Telegram's 50MB limit.\n\n[Download Directly]({download_url})", parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
            return

        status_msg.edit_text("Uploading book to Telegram...")
        
        file_obj = io.BytesIO(content)
        safe_title = re.sub(r'[\\/*?:"<>|]', "", book.title).replace(" ", "_")
        file_obj.name = f"{safe_title}.{selected_format}"
        file_obj.seek(0)
        
        bot.send_document(
            chat_id=query.message.chat_id,
            document=file_obj,
            filename=file_obj.name,
            caption=f"📚 *{book.title}*\n✍️ *Author:* {book.author}\n📄 *Format:* {selected_format.upper()}",
            parse_mode=ParseMode.MARKDOWN,
            timeout=120
        )
        status_msg.delete()
        
    except Exception as e:
        LOGGER.error(f"[ABook DL Error] {e}")
        status_msg.edit_text(f"❌ Download stream failed.\n\n[Mirror Link](https://{book.domain}/md5/{book.md5})", parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


# ==========================================
# MODULE REGISTRATION & HANDLERS
# ==========================================
# /book Handlers
BOOK_HANDLER = DisableAbleCommandHandler("book", book_command, pass_args=True)
OPENLIB_CB_HANDLER = CallbackQueryHandler(openlib_callback, pattern=r"^ol_")

# /abook Handlers
ABOOK_HANDLER = DisableAbleCommandHandler("abook", abook_command, pass_args=True)
ABOOK_MENU_HANDLER = CallbackQueryHandler(abook_menu_callback, pattern=r"^ab_(opt|cancel|back)_")
ABOOK_DL_HANDLER = CallbackQueryHandler(abook_dl_callback, pattern=r"^ab_dl_")

# Dispatcher Registration
dispatcher.add_handler(BOOK_HANDLER)
dispatcher.add_handler(OPENLIB_CB_HANDLER)
dispatcher.add_handler(ABOOK_HANDLER)
dispatcher.add_handler(ABOOK_MENU_HANDLER)
dispatcher.add_handler(ABOOK_DL_HANDLER)

__help__ = """
📚 *Book Downloader Hub*

*Public Domain Library:*
- `/book <title>`: Search and download public domain books & classics safely.

*Anna's Archive Network:*
- `/abook <title>`: Search the global archive network.
- `/abook <title> --format epub`: Filter specific formats.
"""

__mod_name__ = "Books"

LOGGER.info("Books module loaded successfully!")
