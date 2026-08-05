import os
import re
import time
import io
import json
import logging
import math
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
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
LOGGER = logging.getLogger(__name__)
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

# --- Constants & Configuration ---
LIBGEN_DOMAINS = ["libgen.is", "libgen.rs", "libgen.st"]
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8"
})

RETRY_STRATEGY = Retry(total=1, backoff_factor=0.2, status_forcelist=[429, 500, 502, 503, 504], raise_on_status=False)
adapter = HTTPAdapter(max_retries=RETRY_STRATEGY, pool_connections=100, pool_maxsize=100)
session.mount("http://", adapter)
session.mount("https://", adapter)

# --- Global Caches ---
OPENLIB_RESULTS = {}
ABOOK_RESULTS = {}
BOOKINFO_RESULTS = {}
CACHE_TIMESTAMPS = {}

def cleanup_caches():
    """Prevents memory leaks by deleting searches older than 30 minutes."""
    current_time = time.time()
    expired_keys = [msg_id for msg_id, ts in CACHE_TIMESTAMPS.items() if current_time - ts > 1800]
    for msg_id in expired_keys:
        OPENLIB_RESULTS.pop(msg_id, None)
        ABOOK_RESULTS.pop(msg_id, None)
        BOOKINFO_RESULTS.pop(msg_id, None)
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
    
@dataclass
class Book:
    md5: str
    title: str
    author: str
    year: str
    domain: str = ""
    files: List[BookFile] = field(default_factory=list)
    
    def get_available_formats(self) -> List[str]:
        return sorted(set(f.extension.lower() for f in self.files))


# ==========================================
# 1. BOOKINFO ENGINE (Google Books Semantic Search)
# ==========================================
def search_google_books(query: str) -> Optional[List[dict]]:
    """Semantic search for book recommendations and metadata."""
    try:
        resp = session.get("https://www.googleapis.com/books/v1/volumes", params={"q": query, "maxResults": 10}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        books = []
        for item in data.get("items", []):
            vol = item.get("volumeInfo", {})
            title = vol.get("title", "Unknown Title")
            authors = ", ".join(vol.get("authors", ["Unknown Author"]))
            desc = vol.get("description", "No description available.")
            year = vol.get("publishedDate", "")[:4]
            page_count = vol.get("pageCount", "Unknown")
            rating = vol.get("averageRating", "N/A")
            
            # Clean HTML out of description
            desc = re.sub(r'<[^>]+>', '', desc)
            if len(desc) > 500:
                desc = desc[:497] + "..."
                
            books.append({
                "title": title,
                "author": authors,
                "year": year,
                "desc": desc,
                "pages": page_count,
                "rating": rating
            })
        return books
    except Exception as e:
        LOGGER.error(f"[GoogleBooks] Search failed: {e}")
        return None

def build_bookinfo_msg(book: dict) -> str:
    return (
        f"📖 *{book['title']}*\n"
        f"✍️ *Author:* {book['author']}\n"
        f"📅 *Year:* {book['year']}\n"
        f"⭐ *Rating:* {book['rating']}/5 | 📄 *Pages:* {book['pages']}\n\n"
        f"📝 *Summary:*\n_{book['desc']}_"
    )

def build_bookinfo_keyboard(msg_id: int, idx: int, total: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📥 Search & Download on LibGen", callback_data=f"bi_dl_{msg_id}_{idx}")]
    ]
    
    nav_row = []
    if idx > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"bi_pg_{msg_id}_{idx-1}"))
    nav_row.append(InlineKeyboardButton(f"{idx+1}/{total}", callback_data="bi_ignore"))
    if idx < total - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"bi_pg_{msg_id}_{idx+1}"))
        
    if nav_row:
        keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data=f"bi_cancel")])
    return InlineKeyboardMarkup(keyboard)

@run_async
def bookinfo_command(bot, update: Update, args):
    msg = update.effective_message
    if not args:
        msg.reply_text("💡 *Smart Book Search*\n\nSearch by concept, plot, or title to get summaries and recommendations.\nExample: `/bookinfo books about wizards in space`", parse_mode=ParseMode.MARKDOWN)
        return
        
    cleanup_caches()
    query = ' '.join(args).strip()
    status_msg = msg.reply_text(f"🧠 Searching concepts for '{query}'...")
    
    results = search_google_books(query)
    if not results:
        status_msg.edit_text("😕 No recommendations found for that query.")
        return
        
    BOOKINFO_RESULTS[status_msg.message_id] = results
    CACHE_TIMESTAMPS[status_msg.message_id] = time.time()
    
    text = build_bookinfo_msg(results[0])
    kb = build_bookinfo_keyboard(status_msg.message_id, 0, len(results))
    status_msg.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@run_async
def bookinfo_callback(bot, update: Update):
    query = update.callback_query
    data = query.data
    
    if data == "bi_ignore":
        query.answer()
        return
    if data == "bi_cancel":
        query.edit_message_text("❌ Closed.")
        query.answer()
        return
        
    parts = data.split("_")
    action, msg_id, idx = parts[1], int(parts[2]), int(parts[3])
    
    results = BOOKINFO_RESULTS.get(msg_id)
    if not results:
        query.answer("Session expired. Please run /bookinfo again.", show_alert=True)
        return
        
    CACHE_TIMESTAMPS[msg_id] = time.time()
    
    if action == "pg":
        text = build_bookinfo_msg(results[idx])
        kb = build_bookinfo_keyboard(msg_id, idx, len(results))
        query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        
    elif action == "dl":
        book = results[idx]
        search_query = f"{book['title']} {book['author']}"
        query.answer("Searching LibGen...")
        status_msg = query.message.edit_text(f"🔍 Searching LibGen for '{book['title']}'...")
        
        try:
            libgen_books = search_libgen(search_query)
            if not libgen_books:
                status_msg.edit_text(f"😕 '{book['title']}' could not be found on LibGen.")
                return
                
            ABOOK_RESULTS[status_msg.message_id] = libgen_books
            CACHE_TIMESTAMPS[status_msg.message_id] = time.time()
            
            keyboard = []
            for i, lb in enumerate(libgen_books[:10]):
                formats = lb.get_available_formats()
                format_str = ', '.join(f.upper() for f in formats[:2])
                title = lb.title[:25] + "..." if len(lb.title) > 25 else lb.title
                author = lb.author[:15] + "..." if len(lb.author) > 15 else lb.author
                btn_text = f"{i+1}. {title} — {author} [{format_str}]"
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"ab_opt_{status_msg.message_id}_{i}")])
            
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="ab_cancel")])
            status_msg.edit_text(f"📚 Found {len(libgen_books)} match(es). Select to download:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            
        except Exception as e:
            status_msg.edit_text(f"❌ Error bridging to LibGen: {str(e)}")


# ==========================================
# 2. OPEN LIBRARY ENGINE (/book)
# ==========================================
class OpenLibraryFetcher:
    @staticmethod
    def search(query: str) -> Optional[List[dict]]:
        try:
            resp = session.get("https://openlibrary.org/search.json", params={"q": query, "limit": 30}, timeout=15)
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
    msg = update.effective_message
    if not args:
        msg.reply_text("📚 *Open Library Search*\n\nExample: `/book Pride and Prejudice`", parse_mode=ParseMode.MARKDOWN)
        return
        
    cleanup_caches()
    query = ' '.join(args).strip()
    status_msg = msg.reply_text(f"🔍 Searching Open Library for '{query}'...")
    
    results = OpenLibraryFetcher.search(query)
    if not results:
        status_msg.edit_text("😕 No public domain books found for that query.")
        return
        
    OPENLIB_RESULTS[status_msg.message_id] = {"query": query, "results": results}
    CACHE_TIMESTAMPS[status_msg.message_id] = time.time()
    
    kb = build_openlib_keyboard(status_msg.message_id, 0, math.ceil(len(results) / 5), results)
    status_msg.edit_text(f"📚 *Results for '{query}'*\nSelect a book to download:", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@run_async
def openlib_callback(bot, update: Update):
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
        query.answer("Search expired.", show_alert=True)
        return
        
    CACHE_TIMESTAMPS[msg_id] = time.time()
    results = search_data["results"]
    
    if action == "pg":
        page = int(parts[3])
        kb = build_openlib_keyboard(msg_id, page, math.ceil(len(results) / 5), results)
        query.edit_message_text(f"📚 *Results for '{search_data['query']}'*\nSelect a book to download:", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        
    elif action == "dl":
        idx = int(parts[3])
        item = results[idx]
        query.answer("Fetching metadata...")
        status_msg = query.message.edit_text(f"⬇️ Preparing '{item['title']}' from Internet Archive...")
        
        try:
            ia_meta = session.get(f"https://archive.org/metadata/{item['ia_id']}", timeout=10).json()
            dl_url, ext = None, "txt"
            for f in ia_meta.get("files", []):
                if f.get("name", "").endswith(".epub"):
                    dl_url, ext = f"https://archive.org/download/{item['ia_id']}/{f['name']}", "epub"
                    break
            if not dl_url:
                for f in ia_meta.get("files", []):
                    if f.get("name", "").endswith(".pdf"):
                        dl_url, ext = f"https://archive.org/download/{item['ia_id']}/{f['name']}", "pdf"
                        break
            if not dl_url:
                status_msg.edit_text("❌ No EPUB or PDF available for this edition.")
                return
                
            status_msg.edit_text("Downloading file to server...")
            resp = session.get(dl_url, stream=True, timeout=45)
            
            file_obj = io.BytesIO(resp.content)
            safe_title = re.sub(r'[\\/*?:<>|]', '', item['title']).replace(' ', '_')
            file_obj.name = f"{safe_title}.{ext}"
            file_obj.seek(0)
            
            status_msg.edit_text("Uploading to Telegram...")
            bot.send_document(
                chat_id=query.message.chat_id, document=file_obj, filename=file_obj.name,
                caption=f"📚 *{item['title']}*\n✍️ *Author:* {item['author']}\n\n_Downloaded via Open Library_",
                parse_mode=ParseMode.MARKDOWN, timeout=120
            )
            status_msg.delete()
        except Exception as e:
            LOGGER.error(f"[OpenLib DL Error] {e}")
            status_msg.edit_text("❌ Download failed. The file may be unavailable.")


# ==========================================
# 3. LIBGEN ENGINE (/abook)
# ==========================================
def search_libgen(query: str, format_filter: Optional[str] = None) -> List[Book]:
    """Search Library Genesis natively using their JSON API."""
    for domain in LIBGEN_DOMAINS:
        try:
            search_url = f"http://{domain}/search.php"
            resp = session.get(search_url, params={"req": query, "res": 25}, timeout=15)
            if resp.status_code != 200:
                continue
                
            ids = re.findall(r'name="id\[\]" value="(\d+)"', resp.text)
            if not ids:
                continue
                
            json_url = f"http://{domain}/json.php"
            meta_resp = session.get(json_url, params={"ids": ",".join(ids[:20]), "c": "id,title,author,year,md5,extension"}, timeout=15)
            
            if meta_resp.status_code == 200:
                data = meta_resp.json()
                books = []
                
                for item in data:
                    ext = item.get('extension', '').lower()
                    if format_filter and ext != format_filter.lower():
                        continue
                        
                    books.append(Book(
                        md5=item.get('md5'),
                        title=item.get('title', 'Unknown')[:100],
                        author=item.get('author', 'Unknown')[:80],
                        year=item.get('year', ''),
                        domain=domain,
                        files=[BookFile(extension=ext, md5=item.get('md5'))]
                    ))
                
                if books:
                    return books
        except Exception as e:
            continue
            
    raise Exception("Could not reach active library networks. They may be temporarily down.")

def get_libgen_download_url(md5: str) -> Optional[str]:
    """Scrapes the direct GET link from library.lol."""
    try:
        resp = session.get(f"http://library.lol/main/{md5}", timeout=15)
        if resp.status_code == 200:
            match = re.search(r'href="(https?://[^"]+)"[^>]*>GET</a>', resp.text, re.IGNORECASE)
            if match:
                return match.group(1)
    except Exception as e:
        LOGGER.error(f"Failed to get LibGen download link: {e}")
    return None

@run_async
def abook_command(bot, update: Update, args):
    msg = update.effective_message
    if not args:
        msg.reply_text("📚 *Library Search*\n\nExample: `/abook How Linux Works`\nFilter: `/abook Dune --format epub`", parse_mode=ParseMode.MARKDOWN)
        return
    
    cleanup_caches()
    query = ' '.join(args)
    format_filter = None
    
    format_match = re.search(r'--format\s+(\w+)', query, re.IGNORECASE)
    if format_match:
        format_filter = format_match.group(1)
        query = re.sub(r'--format\s+\w+', '', query).strip()
    
    status_msg = msg.reply_text(f"🔍 Searching Library for '{query}'...")
    
    try:
        books = search_libgen(query, format_filter)
        
        if not books:
            status_msg.edit_text("😕 No results found. Try different keywords.")
            return
        
        ABOOK_RESULTS[status_msg.message_id] = books
        CACHE_TIMESTAMPS[status_msg.message_id] = time.time()
        
        keyboard = []
        for i, book in enumerate(books[:10]):
            formats = book.get_available_formats()
            format_str = ', '.join(f.upper() for f in formats[:2])
            title = book.title[:25] + "..." if len(book.title) > 25 else book.title
            author = book.author[:15] + "..." if len(book.author) > 15 else book.author
            
            button_text = f"{i+1}. {title} — {author} [{format_str}]"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"ab_opt_{status_msg.message_id}_{i}")])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="ab_cancel")])
        status_msg.edit_text(f"📚 Found {len(books)} book(s). Select one to download:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        status_msg.edit_text(f"❌ Search failed: {str(e)}")

@run_async
def abook_menu_callback(bot, update: Update):
    query = update.callback_query
    
    if query.data == "ab_cancel":
        query.edit_message_text("❌ Search cancelled.")
        query.answer()
        return

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
            title = book.title[:25] + "..." if len(book.title) > 25 else book.title
            author = book.author[:15] + "..." if len(book.author) > 15 else book.author
            button_text = f"{i+1}. {title} — {author} [{format_str}]"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"ab_opt_{msg_id}_{i}")])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="ab_cancel")])
        query.edit_message_text(f"📚 Found {len(books)} book(s). Select one to download:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        query.answer()
        return

    if query.data.startswith("ab_opt_"):
        parts = query.data.split("_")
        _, _, msg_id_str, idx_str = parts
        msg_id, idx = int(msg_id_str), int(idx_str)
        
        books = ABOOK_RESULTS.get(msg_id)
        if not books or idx >= len(books):
            query.answer("Search expired. Please run /abook again.", show_alert=True)
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
            f"*📖 {book.title}*\n\n"
            f"✍️ *Author:* {book.author}\n"
            f"📅 *Year:* {book.year if book.year else 'N/A'}\n"
            f"Select format to download:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        query.answer()
        return

@run_async
def abook_dl_callback(bot, update: Update):
    query = update.callback_query
    
    try:
        parts = query.data.split("_")
        _, _, msg_id_str, idx_str, fmt_idx_str = parts
        msg_id, idx, fmt_idx = int(msg_id_str), int(idx_str), int(fmt_idx_str)
        
        books = ABOOK_RESULTS.get(msg_id)
        if not books or idx >= len(books):
            query.answer("Search expired.", show_alert=True)
            return
        
        book = books[idx]
        formats = book.get_available_formats()
        selected_format = formats[fmt_idx]
        
        query.answer(f"Downloading {selected_format.upper()}...")
        status_msg = query.message.edit_text(f"⬇️ Locating '{book.title}' [{selected_format.upper()}]...")
        
        download_url = get_libgen_download_url(book.md5)
        
        if not download_url:
            status_msg.edit_text(
                f"❌ Could not extract direct download link.\n\n"
                f"[Try Manual Download](http://library.lol/main/{book.md5})",
                parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
            )
            return
            
        status_msg.edit_text(f"⬇️ Downloading file to server...")
        
        try:
            resp = session.get(download_url, stream=True, timeout=60)
            resp.raise_for_status()
            
            content_length = int(resp.headers.get('content-length', 0))
            if content_length > MAX_FILE_SIZE:
                status_msg.edit_text(f"⚠️ File exceeds Telegram's 50MB limit.\n\n[Download Directly]({download_url})", parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
                return
            
            content = resp.content
            
            if content.startswith(b'<!DOCTYPE') or b'<html' in content[:50]:
                status_msg.edit_text("❌ Download mirror served an error page.")
                return
            
            status_msg.edit_text("📤 Uploading book to Telegram...")
            
            file_obj = io.BytesIO(content)
            safe_title = re.sub(r'[\\/*?:"<>|]', '_', book.title)
            filename = f"{safe_title}.{selected_format}"
            file_obj.name = filename
            
            bot.send_document(
                chat_id=query.message.chat_id,
                document=file_obj,
                filename=filename,
                caption=f"📚 *{book.title}*\n✍️ *Author:* {book.author}\n📄 *Format:* {selected_format.upper()}\n\n_Downloaded via Library Search_",
                parse_mode=ParseMode.MARKDOWN,
                timeout=120
            )
            
            status_msg.delete()
            
        except Exception as e:
            LOGGER.error(f"Download stream error: {e}")
            status_msg.edit_text(f"❌ Download failed.\n\n[Try Direct Link]({download_url})", parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
            
    except Exception as e:
        LOGGER.error(f"Callback error: {e}")
        query.answer("An error occurred.", show_alert=True)


# ==========================================
# MODULE REGISTRATION & HANDLERS
# ==========================================
BOOKINFO_HANDLER = DisableAbleCommandHandler("bookinfo", bookinfo_command, pass_args=True)
BOOKINFO_CB_HANDLER = CallbackQueryHandler(bookinfo_callback, pattern=r"^bi_")

BOOK_HANDLER = DisableAbleCommandHandler("book", book_command, pass_args=True)
OPENLIB_CB_HANDLER = CallbackQueryHandler(openlib_callback, pattern=r"^ol_")

ABOOK_HANDLER = DisableAbleCommandHandler("abook", abook_command, pass_args=True)
ABOOK_MENU_HANDLER = CallbackQueryHandler(abook_menu_callback, pattern=r"^ab_(opt|cancel|back)_")
ABOOK_DL_HANDLER = CallbackQueryHandler(abook_dl_callback, pattern=r"^ab_dl_")

dispatcher.add_handler(BOOKINFO_HANDLER)
dispatcher.add_handler(BOOKINFO_CB_HANDLER)
dispatcher.add_handler(BOOK_HANDLER)
dispatcher.add_handler(OPENLIB_CB_HANDLER)
dispatcher.add_handler(ABOOK_HANDLER)
dispatcher.add_handler(ABOOK_MENU_HANDLER)
dispatcher.add_handler(ABOOK_DL_HANDLER)

__help__ = """
📚 *Book Hub*

*Smart Recommendations:*
- `/bookinfo <query>`: Semantic search for book summaries and recommendations (e.g., "books about time travel").

*Library Search:*
- `/abook <title>`: Search and download books directly.
- `/abook <title> --format epub`: Filter specific formats.

*Public Domain Library:*
- `/book <title>`: Download public domain books & classics safely.
"""

__mod_name__ = "Books"

LOGGER.info("Books module loaded successfully!")
