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
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

# --- Constants & Configuration ---
ANNAS_DOMAINS = ["annas-archive.gs", "annas-archive.li", "annas-archive.se", "annas-archive.org", "annas-archive.gl"]
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

# Session with proper headers to avoid blocking
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1"
})

# --- Global Caches ---
ABOOK_RESULTS = {}
CACHE_TIMESTAMPS = {}

def cleanup_caches():
    """Prevents memory leaks by deleting searches older than 30 minutes."""
    current_time = time.time()
    expired_keys = [msg_id for msg_id, ts in CACHE_TIMESTAMPS.items() if current_time - ts > 1800]
    for msg_id in expired_keys:
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
# IMPROVED ANNA'S ARCHIVE SEARCH
# ==========================================
def search_annas_archive(query: str, format_filter: Optional[str] = None) -> List[Book]:
    """
    Improved search for Anna's Archive using JSON API when available,
    with fallback to HTML parsing.
    """
    params = {"q": query}
    if format_filter:
        params["ext"] = format_filter.lower()
    
    # Try JSON API first (more reliable)
    for domain in ANNAS_DOMAINS:
        try:
            # Try the JSON endpoint
            json_url = f"https://{domain}/search.json"
            resp = session.get(json_url, params=params, timeout=15)
            
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    books = []
                    seen_md5s = set()
                    
                    # Handle different response structures
                    results = data.get('results', data.get('books', []))
                    if not results and 'items' in data:
                        results = data['items']
                    
                    for item in results[:20]:
                        md5 = item.get('md5', item.get('id', ''))
                        if not md5 or md5 in seen_md5s:
                            continue
                        seen_md5s.add(md5)
                        
                        # Extract metadata
                        title = item.get('title', 'Unknown Title')
                        author = item.get('author', 'Unknown Author')
                        year = str(item.get('year', ''))
                        
                        # Extract files
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
                            # If no files listed, try to detect from the item
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
                    # JSON parsing failed, fall back to HTML
                    pass
                    
        except Exception as e:
            LOGGER.debug(f"JSON API failed on {domain}: {e}")
            continue
    
    # Fallback: HTML parsing (for when JSON API is blocked)
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
    """Parse HTML search results from Anna's Archive."""
    books = []
    seen_md5s = set()
    
    # Find all book entries using the md5 pattern
    pattern = r'href="/(?:md5|slow_download|book)/([a-fA-F0-9]{32})"'
    md5_matches = re.finditer(pattern, html_content)
    
    for match in md5_matches:
        md5 = match.group(1)
        if md5 in seen_md5s:
            continue
        seen_md5s.add(md5)
        
        # Get the surrounding context for this book
        start = max(0, match.start() - 200)
        end = min(len(html_content), match.end() + 500)
        context = html_content[start:end]
        
        # Extract title from the context
        title_match = re.search(r'<h[1-3][^>]*>([^<]+)</h[1-3]>', context)
        if not title_match:
            # Try to find the title in the anchor text
            title_match = re.search(rf'href="/(?:md5|slow_download|book)/{md5}"[^>]*>([^<]+)</a>', html_content[max(0, match.start()-100):min(len(html_content), match.end()+100)])
        
        title = "Unknown Title"
        author = "Unknown Author"
        year = ""
        
        if title_match:
            title = html.unescape(re.sub(r'<[^>]+>', '', title_match.group(1))).strip()
        
        # Try to find author in the context
        author_patterns = [
            r'<div[^>]*class="[^"]*author[^"]*"[^>]*>([^<]+)</div>',
            r'<span[^>]*class="[^"]*author[^"]*"[^>]*>([^<]+)</span>',
            r'by\s+([^<]+?)(?:\s*[,<]|\s*$)',
        ]
        
        for pattern in author_patterns:
            author_match = re.search(pattern, context, re.IGNORECASE)
            if author_match:
                author = html.unescape(author_match.group(1).strip())
                break
        
        # Find year
        year_match = re.search(r'\b(19\d{2}|20\d{2})\b', context)
        if year_match:
            year = year_match.group(1)
        
        # Find available formats
        formats = set()
        format_patterns = [
            r'\b(pdf)\b', r'\b(epub)\b', r'\b(mobi)\b', 
            r'\b(azw3)\b', r'\b(djvu)\b', r'\b(fb2)\b',
            r'\b(docx)\b', r'\b(txt)\b'
        ]
        
        for fmt_pattern in format_patterns:
            if re.search(fmt_pattern, context, re.IGNORECASE):
                fmt = re.search(fmt_pattern, context, re.IGNORECASE).group(1).lower()
                formats.add(fmt)
        
        if not formats:
            formats = {"epub"}
        
        files = [BookFile(extension=fmt) for fmt in formats]
        
        # Clean up title and author
        title = title.replace('\n', ' ').strip()
        author = author.replace('\n', ' ').strip()
        
        # Skip if title is too short or looks like garbage
        if len(title) < 2 or title in ['Download', 'Cover', 'Title']:
            continue
            
        book = Book(
            md5=md5,
            title=title[:100],
            author=author[:80] if author and author != 'Unknown Author' else 'Unknown Author',
            year=year,
            domain=domain,
            files=files
        )
        books.append(book)
        
        if len(books) >= 20:
            break
    
    return books

def get_abook_download_url(md5: str, known_working_domain: str) -> Optional[str]:
    """
    Get a direct download URL from Anna's Archive.
    Uses the md5 page to find the actual download link.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://annas-archive.org/"
    }
    
    domains_to_try = [known_working_domain] + [d for d in ANNAS_DOMAINS if d != known_working_domain]
    
    for domain in domains_to_try:
        try:
            # First, try to get the download URL from the md5 page
            md5_page_url = f"https://{domain}/md5/{md5}"
            resp = session.get(md5_page_url, headers=headers, timeout=15)
            
            if resp.status_code == 200:
                # Look for download links
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
                
                # If no direct download link, try the slow_download endpoint
                slow_url = f"https://{domain}/slow_download/{md5}"
                slow_resp = session.get(slow_url, headers=headers, timeout=15, allow_redirects=True)
                
                if slow_resp.status_code == 200:
                    # Look for download link in the slow download page
                    for pattern in download_patterns:
                        dl_match = re.search(pattern, slow_resp.text, re.IGNORECASE)
                        if dl_match:
                            url = dl_match.group(1)
                            if url and 'annas-archive' not in url.lower():
                                return url
                
                # If still no link, return the md5 page URL for manual download
                return md5_page_url
                
        except Exception as e:
            LOGGER.debug(f"Failed to get download URL from {domain}: {e}")
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
    
    # Check for format filter
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
        
        # Store results
        ABOOK_RESULTS[status_msg.message_id] = books
        CACHE_TIMESTAMPS[status_msg.message_id] = time.time()
        
        # Build keyboard with book info
        keyboard = []
        for i, book in enumerate(books[:10]):
            # Get formats for display
            formats = book.get_available_formats()
            format_str = ', '.join(f.upper() for f in formats[:2])
            if len(formats) > 2:
                format_str += f" +{len(formats)-2}"
            
            # Truncate title and author for display
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

    # Handle back button
    if query.data.startswith("ab_back_"):
        _, msg_id_str = query.data.split("_", 2)[1:]
        msg_id = int(msg_id_str)
        books = ABOOK_RESULTS.get(msg_id)
        
        if not books:
            query.answer("Search expired. Please run /abook again.", show_alert=True)
            return
        
        # Rebuild the search results keyboard
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

    # Handle book selection
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
        
        # Build format selection keyboard
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
        
        # Get download URL
        download_url = get_abook_download_url(book.md5, book.domain)
        
        if not download_url:
            status_msg.edit_text(
                f"❌ Could not find a working download link.\n\n"
                f"Try opening in browser:\n"
                f"https://{book.domain}/md5/{book.md5}",
                disable_web_page_preview=True
            )
            return
        
        # Try to download
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/octet-stream,*/*",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Referer": f"https://{book.domain}/"
            }
            
            # Stream download with progress indication
            resp = session.get(download_url, headers=headers, stream=True, timeout=60)
            resp.raise_for_status()
            
            # Check content length
            content_length = int(resp.headers.get('content-length', 0))
            if content_length > MAX_FILE_SIZE:
                status_msg.edit_text(
                    f"⚠️ File exceeds Telegram's 50MB limit.\n\n"
                    f"[Download Directly]({download_url})",
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True
                )
                return
            
            # Download content
            content = resp.content
            
            # Check if it's an HTML page (blocked request)
            if content.startswith(b'<!DOCTYPE') or b'captcha' in content[:1000].lower():
                status_msg.edit_text(
                    f"❌ Mirror is blocking downloads.\n\n"
                    f"Try opening in browser:\n"
                    f"https://{book.domain}/md5/{book.md5}",
                    disable_web_page_preview=True
                )
                return
            
            # Upload to Telegram
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
                       f"*Format:* {selected_format.upper()}\n"
                       f"*Size:* {len(content)/(1024*1024):.2f} MB\n\n"
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
            LOGGER.error(f"Download request error: {e}")
            status_msg.edit_text(
                f"❌ Download failed.\n\n"
                f"[Try Direct Link]({download_url})",
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
        except Exception as e:
            LOGGER.error(f"Download error: {e}", exc_info=True)
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
# MODULE REGISTRATION
# ==========================================
ABOOK_HANDLER = DisableAbleCommandHandler("abook", abook_command, pass_args=True)
ABOOK_MENU_HANDLER = CallbackQueryHandler(abook_menu_callback, pattern=r"^ab_(opt|cancel|back)_")
ABOOK_DL_HANDLER = CallbackQueryHandler(abook_dl_callback, pattern=r"^ab_dl_")

dispatcher.add_handler(ABOOK_HANDLER)
dispatcher.add_handler(ABOOK_MENU_HANDLER)
dispatcher.add_handler(ABOOK_DL_HANDLER)

__help__ = """
📚 *Book Downloader (Anna's Archive)*

Search and download books from Anna's Archive.

*Commands:*
- `/abook <title>`: Search for books
- `/abook <title> --format <format>`: Filter by format

*Supported formats:* PDF, EPUB, MOBI, AZW3, DJVU

*Examples:*
- `/abook How Linux Works`
- `/abook Dune --format epub`
"""

__mod_name__ = "Anna's Archive"

LOGGER.info("Anna's Archive module loaded with improved search!")
