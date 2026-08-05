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
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
LOGGER = logging.getLogger(__name__)

# SILENCE NOISY DNS RETRY LOGS FOR DEAD MIRRORS
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

# --- Constants & Configuration ---
ANNAS_DOMAINS = ["annas-archive.gs", "annas-archive.li", "annas-archive.se", "annas-archive.org", "annas-archive.gl"]
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

# Fast-fail retries so dead domains are skipped instantly
RETRY_STRATEGY = Retry(
    total=1,
    backoff_factor=0.2,
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
    domain: str = ""
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
    """
    Improved search for Anna's Archive using JSON API when available,
    with fallback to indestructible HTML parsing.
    """
    params = {"q": query}
    if format_filter:
        params["ext"] = format_filter.lower()
    
    # Try JSON API first (more reliable)
    for domain in ANNAS_DOMAINS:
        try:
            json_url = f"https://{domain}/search.json"
            resp = session.get(json_url, params=params, timeout=15)
            
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    books = []
                    seen_md5s = set()
                    
                    results = data.get('results', data.get('books', []))
                    if not results and 'items' in data:
                        results = data['items']
                    
                    for item in results[:20]:
                        md5 = item.get('md5', item.get('id', ''))
                        if not md5 or md5 in seen_md5s:
                            continue
                        seen_md5s.add(md5)
                        
                        title = item.get('title', 'Unknown Title')
                        author = item.get('author', 'Unknown Author')
                        year = str(item.get('year', ''))
                        
                        files = []
                        file_list = item.get('files', [])
                        if file_list:
                            for f in file_list:
                                ext = f.get('extension', 'epub')
                                files.append(BookFile(
                                    extension=ext,
                                    size=f.get('size', 0),
                                    md5=f.get('md5', md5),
                                    quality=f.get('quality', 'standard')
                                ))
                        else:
                            ext = item.get('extension', 'epub')
                            files.append(BookFile(extension=ext))
                        
                        book = Book(
                            md5=md5,
                            title=title[:100],
                            author=author[:80],
                            year=year,
                            domain=domain,
                            files=files,
                            publisher=item.get('publisher', ''),
                            language=item.get('language', ''),
                            pages=item.get('pages', 0)
                        )
                        books.append(book)
                    
                    if books:
                        LOGGER.info(f"Found {len(books)} books via JSON API on {domain}")
                        return books
                        
                except json.JSONDecodeError:
                    pass
                    
        except Exception as e:
            continue
    
    # Fallback: Indestructible HTML parsing
    for domain in ANNAS_DOMAINS:
        try:
            resp = session.get(f"https://{domain}/search", params=params, timeout=15)
            if resp.status_code == 200:
                books = parse_html_search_results(resp.text, domain)
                if books:
                    LOGGER.info(f"Found {len(books)} books via HTML parsing on {domain}")
                    return books
        except Exception:
            continue
    
    raise Exception("Could not reach active archive networks or no results found.")

def parse_html_search_results(html_content: str, domain: str) -> List[Book]:
    """Bulletproof HTML parsing using cleanly isolated anchor tags."""
    books = []
    seen_md5s = set()
    
    # Match the entire anchor block enclosing each book result
    pattern = r'<a[^>]*href="/(?:md5|slow_download|book)/([a-fA-F0-9]{32})"[^>]*>(.*?)</a>'
    
    for match in re.finditer(pattern, html_content, re.IGNORECASE | re.DOTALL):
        md5 = match.group(1)
        if md5 in seen_md5s:
            continue
        seen_md5s.add(md5)
        
        inner_html = match.group(2)
        
        # Strip out images and their alt text to prevent title pollution
        inner_html = re.sub(r'<img[^>]*>', '', inner_html, flags=re.IGNORECASE)
        
        # Strip all remaining HTML tags
        raw_text = html.unescape(re.sub(r'<[^>]+>', '\n', inner_html))
        
        # Clean lines
        lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
        
        clean_lines = []
        for line in lines:
            ll = line.lower()
            if not any(x in ll for x in ["download", "mb", "kb", "gb", "pdf", "epub", "mobi", "azw3", "djvu", "cover", "english", "spanish"]):
                clean_lines.append(line)
                
        title = "Unknown Title"
        author = "Unknown Author"
        
        if len(clean_lines) > 0:
            title = clean_lines[0]
        if len(clean_lines) > 1:
            author = clean_lines[1]
            
        formats = set(f.lower() for f in re.findall(r'\b(pdf|epub|mobi|azw3|djvu)\b', inner_html, re.IGNORECASE))
        if not formats:
            formats = {"epub"}
            
        year_match = re.search(r'\b(19\d{2}|20\d{2})\b', inner_html)
        year = year_match.group(1) if year_match else ""
            
        files = [BookFile(extension=fmt) for fmt in formats]
        
        if len(title) < 2 or title.lower() == 'unknown title':
            continue
            
        books.append(Book(
            md5=md5,
            title=title[:100],
            author=author[:80] if author else "Unknown Author",
            year=year,
            domain=domain,
            files=files
        ))
        
        if len(books) >= 20:
            break
            
    return books

def get_abook_download_url(md5: str, known_working_domain: str) -> Optional[str]:
    """Get a direct download URL from Anna's Archive."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://annas-archive.org/"
    }
    
    domains_to_try = [known_working_domain] + [d for d in ANNAS_DOMAINS if d != known_working_domain]
    
    for domain in domains_to_try:
        try:
            md5_page_url = f"https://{domain}/md5/{md5}"
            resp = session.get(md5_page_url, headers=headers, timeout=15)
            
            if resp.status_code == 200:
                download_patterns = [
                    r'href="(https?://[^"]+\.(?:epub|pdf|mobi|azw3|djvu))"',
                    r'<a[^>]*class="[^"]*download[^"]*"[^>]*href="([^"]+)"',
                    r'href="(https?://[^"]+library\.lol[^"]+)"',
                    r'href="(https?://[^"]+libgen[^"]+)"',
                    r'href="(https?://[^"]+ipfs[^"]+)"',
                ]
                
                for pattern in download_patterns:
                    dl_match = re.search(pattern, resp.text, re.IGNORECASE)
                    if dl_match:
                        url = dl_match.group(1)
                        if url and 'annas-archive' not in url.lower():
                            return url
                
                slow_url = f"https://{domain}/slow_download/{md5}"
                slow_resp = session.get(slow_url, headers=headers, timeout=15, allow_redirects=True)
                
                if slow_resp.status_code == 200:
                    for pattern in download_patterns:
                        dl_match = re.search(pattern, slow_resp.text, re.IGNORECASE)
                        if dl_match:
                            url = dl_match.group(1)
                            if url and 'annas-archive' not in url.lower():
                                return url
                
                return md5_page_url
                
        except Exception as e:
            continue
    
    return None


# ==========================================
# TELEGRAM HANDLERS
# ==========================================
@run_async
def abook_command(bot, update: Update, args):
    """Search for books on Anna's Archive."""
    msg = update.effective_message
    if not args:
        msg.reply_text(
            "📚 *Anna's Archive Search*\n\n"
            "Search and download any book directly.\n"
            "Example: `/abook How Linux Works`\n\n"
            "Filter by format:\n"
            "`/abook How Linux Works --format epub`",
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
            status_msg.edit_text("😕 No results found. Try different keywords or check your spelling.")
            return
        
        ABOOK_RESULTS[status_msg.message_id] = books
        CACHE_TIMESTAMPS[status_msg.message_id] = time.time()
        
        keyboard = []
        for i, book in enumerate(books[:10]):
            formats = book.get_available_formats()
            format_str = ', '.join(f.upper() for f in formats[:2])
            if len(formats) > 2:
                format_str += f" +{len(formats)-2}"
            
            title = book.title[:25] + "..." if len(book.title) > 25 else book.title
            author = book.author[:15] + "..." if len(book.author) > 15 else book.author
            
            button_text = f"{i+1}. {title} — {author} [{format_str}]"
            if len(button_text) > 60:
                button_text = button_text[:57] + "..."
                
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"ab_opt_{status_msg.message_id}_{i}")])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="ab_cancel")])
        
        status_msg.edit_text(
            f"📚 Found {len(books)} book(s). Select one to download:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        LOGGER.error(f"ABook Search error: {e}", exc_info=True)
        status_msg.edit_text(f"❌ Search failed: {str(e)}")

@run_async
def abook_menu_callback(bot, update: Update):
    """Handle Anna's Archive book selection menus."""
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
            query.answer("Search expired. Please run /abook again.", show_alert=True)
            return
        
        keyboard = []
        for i, book in enumerate(books[:10]):
            formats = book.get_available_formats()
            format_str = ', '.join(f.upper() for f in formats[:2])
            if len(formats) > 2:
                format_str += f" +{len(formats)-2}"
            title = book.title[:25] + "..." if len(book.title) > 25 else book.title
            author = book.author[:15] + "..." if len(book.author) > 15 else book.author
            button_text = f"{i+1}. {title} — {author} [{format_str}]"
            if len(button_text) > 60:
                button_text = button_text[:57] + "..."
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"ab_opt_{msg_id}_{i}")])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="ab_cancel")])
        
        query.edit_message_text(
            f"📚 Found {len(books)} book(s). Select one to download:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        query.answer()
        return

    if query.data.startswith("ab_opt_"):
        parts = query.data.split("_")
        if len(parts) != 4:
            query.answer("Invalid selection.", show_alert=True)
            return
            
        _, _, msg_id_str, idx_str = parts
        msg_id, idx = int(msg_id_str), int(idx_str)
        
        books = ABOOK_RESULTS.get(msg_id)
        if not books or idx >= len(books):
            query.answer("Search expired. Please run /abook again.", show_alert=True)
            return
        
        book = books[idx]
        formats = book.get_available_formats()
        
        if not formats:
            query.answer("No downloadable formats available.", show_alert=True)
            return
        
        keyboard = []
        row_btns = []
        for fmt_idx, fmt in enumerate(formats):
            btn = InlineKeyboardButton(
                f"📄 {fmt.upper()}", 
                callback_data=f"ab_dl_{msg_id}_{idx}_{fmt_idx}"
            )
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
            f"📄 *Formats:* {', '.join(f.upper() for f in formats)}\n\n"
            f"Select format to download:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        query.answer()
        return

@run_async
def abook_dl_callback(bot, update: Update):
    """Handle actual download execution for Anna's Archive."""
    query = update.callback_query
    
    try:
        parts = query.data.split("_")
        if len(parts) != 5:
            query.answer("Invalid download request.", show_alert=True)
            return
            
        _, _, msg_id_str, idx_str, fmt_idx_str = parts
        msg_id, idx, fmt_idx = int(msg_id_str), int(idx_str), int(fmt_idx_str)
        
        books = ABOOK_RESULTS.get(msg_id)
        if not books or idx >= len(books):
            query.answer("Search expired. Please run /abook again.", show_alert=True)
            return
        
        book = books[idx]
        formats = book.get_available_formats()
        if fmt_idx >= len(formats):
            query.answer("Format unavailable.", show_alert=True)
            return
            
        selected_format = formats[fmt_idx]
        
        query.answer(f"Downloading {selected_format.upper()}...")
        status_msg = query.message.edit_text(
            f"⬇️ Downloading '{book.title}' [{selected_format.upper()}]...\n"
            f"This may take a moment."
        )
        
        download_url = get_abook_download_url(book.md5, book.domain)
        
        if not download_url:
            status_msg.edit_text(
                f"❌ Could not find a working download link.\n\n"
                f"Try opening in browser:\n"
                f"https://{book.domain}/md5/{book.md5}",
                disable_web_page_preview=True
            )
            return
        
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/octet-stream,*/*",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Referer": f"https://{book.domain}/"
            }
            
            resp = session.get(download_url, headers=headers, stream=True, timeout=60)
            resp.raise_for_status()
            
            content = resp.content
            
            # Guard against HTML error pages and Captchas being saved as books
            if content.startswith(b'<!DOCTYPE') or content.startswith(b'<html') or b'<body' in content[:500].lower() or b'captcha' in content[:1000].lower():
                status_msg.edit_text(
                    f"❌ Mirror is blocking downloads.\n\n"
                    f"Try opening in browser:\n"
                    f"https://{book.domain}/md5/{book.md5}",
                    disable_web_page_preview=True
                )
                return

            if len(content) > MAX_FILE_SIZE:
                status_msg.edit_text(
                    f"⚠️ File exceeds Telegram's 50MB limit.\n\n"
                    f"[Download Directly]({download_url})",
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True
                )
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
                caption=f"📚 *{book.title}*\n"
                        f"✍️ *Author:* {book.author}\n"
                        f"📄 *Format:* {selected_format.upper()}\n"
                        f"📦 *Size:* {len(content)/(1024*1024):.2f} MB\n\n"
                        f"_Downloaded from Anna's Archive_",
                parse_mode=ParseMode.MARKDOWN,
                timeout=120
            )
            
            status_msg.delete()
            
        except requests.exceptions.Timeout:
            status_msg.edit_text(
                f"⏰ Download timed out.\n\n"
                f"[Try Direct Link]({download_url})",
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
        except requests.exceptions.RequestException as e:
            status_msg.edit_text(
                f"❌ Download failed.\n\n"
                f"[Try Direct Link]({download_url})",
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
        except Exception as e:
            status_msg.edit_text(
                f"❌ An error occurred during download.\n\n"
                f"[Try Direct Link]({download_url})",
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
            
    except Exception as e:
        LOGGER.error(f"Callback error: {e}", exc_info=True)
        query.answer("An error occurred.", show_alert=True)


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
