import os
import re
import time
import io
import json
import html
import logging
import math
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3
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

# SILENCE NOISY DNS/SSL WARNINGS FOR DEAD/EXPIRED MIRRORS
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Constants & Configuration ---
GOOGLE_BOOKS_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY")

# Classic domains (libgen.is/rs/st) and library.lol went offline in late 2025.
# The surviving mirrors all run the "libgen+" format-2 interface, which uses
# index.php?req= for search, /ads.php?md5= for the download page and
# get.php?md5=...&key= for the actual file. Rotate through the live ones.
LIBGEN_DOMAINS = ["libgen.li", "libgen.la", "libgen.gl", "libgen.bz", "libgen.vg"]
# Anna's Archive mirrors. .org and .se frequently fail DNS resolution from
# cloud hosts (Render) and some ISPs; .gl resolves and serves search results
# without a JS challenge, so it is listed first.
ANNA_MIRRORS = ["annas-archive.gl", "annas-archive.org", "annas-archive.se"]
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
AABOOK_RESULTS = {}
CACHE_TIMESTAMPS = {}

def cleanup_caches():
    """Prevents memory leaks by deleting searches older than 30 minutes."""
    current_time = time.time()
    expired_keys = [msg_id for msg_id, ts in CACHE_TIMESTAMPS.items() if current_time - ts > 1800]
    for msg_id in expired_keys:
        OPENLIB_RESULTS.pop(msg_id, None)
        ABOOK_RESULTS.pop(msg_id, None)
        BOOKINFO_RESULTS.pop(msg_id, None)
        AABOOK_RESULTS.pop(msg_id, None)
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
# LIBGEN FORMAT-2 HELPERS (current mirrors)
# ==========================================
def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<[^>]+>', ' ', text)
    return html.unescape(text).strip()

def _parse_size(size_text: str) -> int:
    """Parse '2 MB' / '1.5 GB' style sizes into bytes."""
    match = re.search(r'([\d.]+)\s*(KB|MB|GB)', size_text, re.IGNORECASE)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2).upper()
    multiplier = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}.get(unit, 1)
    return int(value * multiplier)

def _parse_libgen_rows(page: str, domain: str) -> List[Book]:
    """Parse the format-2 search results table (one <tr> per file row)."""
    books = []
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', page, re.S | re.I):
        if "ads.php?md5=" not in row:
            continue

        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)
        if len(cells) < 8:
            continue

        md5_match = re.search(r'ads\.php\?md5=([a-f0-9]{32})', row, re.I)
        if not md5_match:
            continue
        md5 = md5_match.group(1)

        # Title lives inside the edition.php anchor
        title_match = re.search(r'edition\.php\?id=\d+[^>]*>(.*?)</a>', cells[0], re.S | re.I)
        title = _strip_html(title_match.group(1)) if title_match else _strip_html(cells[0])
        # Drop any trailing badge noise like "l 2478395"
        title = re.sub(r'\s*l\s+\d+$', '', title).strip()

        ext = _strip_html(cells[7]).split()[0].lower() if len(cells) > 7 else ""
        size = _parse_size(_strip_html(cells[6])) if len(cells) > 6 else 0

        books.append(Book(
            md5=md5,
            title=title[:100],
            author=_strip_html(cells[1])[:80] if len(cells) > 1 else "Unknown",
            year=_strip_html(cells[3])[:8] if len(cells) > 3 else "",
            domain=domain,
            files=[BookFile(extension=ext, size=size, md5=md5)]
        ))
    return books


# ==========================================
# 4. ANNA'S ARCHIVE ENGINE (/aabook)
# ==========================================
def _parse_annas_rows(page: str) -> List[dict]:
    """Parse Anna's Archive search results (one flex row per book)."""
    books = []
    for seg in re.split(r'(?=<div class="flex\s+pt-3 pb-3 border-b)', page):
        md5_match = re.search(r'/md5/([a-f0-9]{32})', seg)
        if not md5_match:
            continue
        md5 = md5_match.group(1)

        title_match = re.search(r'class="[^"]*js-vim-focus[^"]*"[^>]*>(.*?)</a>', seg, re.S)
        title = _strip_html(title_match.group(1)) if title_match else "Unknown"

        author_match = re.search(r'icon-\[mdi--user-edit\][^>]*></span>\s*(.*?)</a>', seg, re.S)
        author = _strip_html(author_match.group(1)) if author_match else "Unknown"

        meta_match = re.search(r'class="text-gray-800[^"]*mt-2">(.*?)(?:\s*<a href="#"|</div>)', seg, re.S)
        meta = _strip_html(meta_match.group(1)) if meta_match else ""

        parts = [p.strip() for p in meta.split('·')]
        ext = next((t for t in parts if re.fullmatch(r'[A-Za-z0-9]{3,5}', t)), "").lower()
        year = next((t for t in parts if re.fullmatch(r'\d{4}', t)), "")
        lang = parts[0].replace('✅', '').replace('❌', '').strip() if parts else ""

        books.append({
            "md5": md5,
            "title": title[:100],
            "author": author[:80],
            "year": year,
            "ext": ext,
            "size": _parse_size(meta),
            "lang": lang
        })
    return books

def search_annas_archive(query: str) -> Optional[List[dict]]:
    """Search Anna's Archive (indexes LibGen, Z-Library, IA, Sci-Hub & more)."""
    for domain in ANNA_MIRRORS:
        try:
            resp = session.get(f"https://{domain}/search", params={"q": query, "lang": "en"}, timeout=20)
            if resp.status_code != 200:
                continue
            if "cloudflare" in resp.text.lower() or "just a moment" in resp.text.lower():
                continue
            books = _parse_annas_rows(resp.text)
            if books:
                return books
            return []
        except Exception as e:
            LOGGER.debug(f"[AnnasArchive] {domain} failed: {e}")
            continue
    raise Exception("Could not reach Anna's Archive. They may be temporarily down.")

# Internet Archive collection/category ids that appear in Anna's Archive
# /md5/ pages but are NOT real item identifiers. Resolving these returns
# generic collection metadata and never a file, so skip them.
IA_JUNK_IDS = {
    "inlibrary", "in_library", "internetarchivebooks", "printdisabled",
    "bannedcollection", "booksfromparliament", "americana",
    "lendinglibrary", "popularlibrary", "top4collection",
    "library_of_congress", "tessellation", "europeanlibraries",
    "web-books", "pub_elements", "cover", "ia_thumb",
}


def get_ia_download_url(md5: str) -> Optional[str]:
    """Resolve an Internet Archive download link for an Anna's Archive record.
    Iterates the mirror list so a single dead mirror (e.g. .org failing DNS)
    doesn't break the whole lookup, and skips IA collection IDs that are not
    real items."""
    for domain in ANNA_MIRRORS:
        try:
            resp = session.get(f"https://{domain}/md5/{md5}", timeout=20)
            if resp.status_code != 200:
                continue
            ia_ids = re.findall(r'https://archive\.org/details/([A-Za-z0-9_.-]+)', resp.text)
            for ia_id in dict.fromkeys(ia_ids):
                if ia_id in IA_JUNK_IDS:
                    continue
                try:
                    meta = session.get(f"https://archive.org/metadata/{ia_id}", timeout=10).json()
                    # Lending-only items return 403 on direct file download — skip them
                    if meta.get("metadata", {}).get("access-restricted-item") == "true":
                        continue
                    for f in meta.get("files", []):
                        name = f.get("name", "")
                        if name.lower().endswith(".epub"):
                            return f"https://archive.org/download/{ia_id}/{name}"
                    for f in meta.get("files", []):
                        name = f.get("name", "")
                        if name.lower().endswith(".pdf") and "encrypted" not in name.lower():
                            return f"https://archive.org/download/{ia_id}/{name}"
                except Exception:
                    continue
        except Exception as e:
            LOGGER.debug(f"[AnnasArchive IA resolver] {domain} failed: {e}")
            continue
    return None

def build_aabook_keyboard(msg_id: int, results: list) -> InlineKeyboardMarkup:
    keyboard = []
    for i, book in enumerate(results[:10]):
        title = book["title"][:25] + "..." if len(book["title"]) > 25 else book["title"]
        author = book["author"][:15] + "..." if len(book["author"]) > 15 else book["author"]
        ext_str = book["ext"].upper() if book["ext"] else "?"
        keyboard.append([InlineKeyboardButton(f"{i+1}. {title} — {author} [{ext_str}]", callback_data=f"aa_opt_{msg_id}_{i}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="aa_cancel")])
    return InlineKeyboardMarkup(keyboard)

def _send_book_file(bot, chat_id: int, status_msg, content: bytes, filename: str,
                    title: str, author: str, ext: str):
    file_obj = io.BytesIO(content)
    file_obj.name = filename
    username = getattr(bot, "username", None) or "the bot"
    credit = f"@{username}" if username != "the bot" else "the bot"
    bot.send_document(
        chat_id=chat_id,
        document=file_obj,
        filename=filename,
        caption=f"📚 *{title}*\n✍️ *Author:* {author}\n📄 *Format:* {ext.upper()}\n\n_Downloaded Via {credit}_",
        parse_mode=ParseMode.MARKDOWN,
        timeout=120
    )
    status_msg.delete()

@run_async
def aabook_command(bot, update: Update, args):
    msg = update.effective_message
    if not args:
        msg.reply_text("📚 *Anna's Archive Search*\n\nA mega-index covering LibGen, Z-Library, Internet Archive & more.\nExample: `/aabook Stephen Hawking`", parse_mode=ParseMode.MARKDOWN)
        return

    cleanup_caches()
    query = ' '.join(args).strip()
    status_msg = msg.reply_text(f"🔍 Searching Anna's Archive for '{query}'...")

    try:
        results = search_annas_archive(query)
        if not results:
            status_msg.edit_text("😕 No results found on Anna's Archive.")
            return

        AABOOK_RESULTS[status_msg.message_id] = results
        CACHE_TIMESTAMPS[status_msg.message_id] = time.time()

        status_msg.edit_text(f"📚 *Results from Anna's Archive ({len(results)} found)*\nSelect a book to download:",
                             reply_markup=build_aabook_keyboard(status_msg.message_id, results),
                             parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        status_msg.edit_text(f"❌ Search failed: {str(e)}")

@run_async
def aabook_callback(bot, update: Update):
    query = update.callback_query
    data = query.data

    if data == "aa_cancel":
        query.edit_message_text("❌ Search cancelled.")
        query.answer()
        return

    if data.startswith("aa_opt_"):
        parts = data.split("_")
        _, _, msg_id_str, idx_str = parts
        msg_id, idx = int(msg_id_str), int(idx_str)

        results = AABOOK_RESULTS.get(msg_id)
        if not results or idx >= len(results):
            query.answer("Search expired. Please run /aabook again.", show_alert=True)
            return

        book = results[idx]
        md5 = book["md5"]
        chat_id = query.message.chat_id

        query.answer("Fetching download...")
        status_msg = query.message.edit_text(f"⬇️ Locating '{book['title']}'...")

        # 1) LibGen CDN first — fastest, works for all libgen-sourced files
        download_url = get_libgen_download_url(md5)
        if download_url:
            try:
                status_msg.edit_text("⬇️ Downloading from LibGen mirror...")
                resp = session.get(download_url, stream=True, timeout=60, verify=False)
                resp.raise_for_status()
                content_length = int(resp.headers.get('content-length', 0))
                if content_length > MAX_FILE_SIZE:
                    status_msg.edit_text(f"⚠️ File exceeds Telegram's 50MB limit.\n\n[Download Directly]({download_url})", parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
                    return
                content = resp.content
                if not (content.startswith(b'<!DOCTYPE') or b'<html' in content[:50]):
                    status_msg.edit_text("📤 Uploading book to Telegram...")
                    safe_title = re.sub(r'[\\/*?:"<>|]', '_', book['title'])
                    _send_book_file(bot, chat_id, status_msg, content, f"{safe_title}.{book['ext']}",
                                    book['title'], book['author'], book['ext'])
                    return
            except Exception as e:
                LOGGER.error(f"[AA LibGen DL] {e}")

        # 2) Internet Archive — covers IA-scanned books missing from LibGen
        ia_url = get_ia_download_url(md5)
        if ia_url:
            try:
                status_msg.edit_text("⬇️ Downloading from Internet Archive...")
                resp = session.get(ia_url, stream=True, timeout=45)
                resp.raise_for_status()
                content_length = int(resp.headers.get('content-length', 0))
                if content_length > MAX_FILE_SIZE:
                    status_msg.edit_text(f"⚠️ File exceeds Telegram's 50MB limit.\n\n[Download Directly]({ia_url})", parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
                    return
                content = resp.content
                if not (content.startswith(b'<!DOCTYPE') or b'<html' in content[:50]):
                    status_msg.edit_text("📤 Uploading book to Telegram...")
                    safe_title = re.sub(r'[\\/*?:"<>|]', '_', book['title'])
                    ext = ia_url.rsplit('.', 1)[-1].split('?')[0]
                    _send_book_file(bot, chat_id, status_msg, content, f"{safe_title}.{ext}",
                                    book['title'], book['author'], ext)
                    return
            except Exception as e:
                LOGGER.error(f"[AA IA DL] {e}")

        status_msg.edit_text(
            f"❌ Could not resolve a direct download link.\n\n"
            f"[Open on Anna's Archive](https://annas-archive.gl/md5/{md5})",
            parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
        )


# ==========================================
# 1. BOOKINFO ENGINE (Google Books Semantic Search)
# ==========================================
def search_google_books(query: str) -> Optional[List[dict]]:
    """Semantic search for book recommendations and metadata."""
    try:
        params = {"q": query, "maxResults": 10}
        if GOOGLE_BOOKS_API_KEY:
            params["key"] = GOOGLE_BOOKS_API_KEY

        resp = session.get("https://www.googleapis.com/books/v1/volumes", params=params, timeout=15)
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

        # LibGen is extremely strict. Subtitles break searches.
        # We strip everything after a colon or parenthesis to get the core title.
        clean_title = book['title'].split(':')[0].split('(')[0].strip()

        query.answer("Searching LibGen...")
        status_msg = query.message.edit_text(f"🔍 Searching LibGen for '{clean_title}'...")

        try:
            libgen_books = search_libgen(clean_title)
            if not libgen_books:
                status_msg.edit_text(f"😕 '{clean_title}' could not be found on LibGen.")
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
            safe_title = re.sub(r'[\\/*?:"<>|]', '', item['title']).replace(' ', '_')
            file_obj.name = f"{safe_title}.{ext}"
            file_obj.seek(0)

            status_msg.edit_text("Uploading to Telegram...")
            username = getattr(bot, "username", None) or "the bot"
            credit = f"@{username}" if username != "the bot" else "the bot"
            bot.send_document(
                chat_id=query.message.chat_id, document=file_obj, filename=file_obj.name,
                caption=f"📚 *{item['title']}*\n✍️ *Author:* {item['author']}\n\n_Downloaded Via {credit}_",
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
    """Search Library Genesis format-2 mirrors using verify=False to bypass expired certificates."""
    for domain in LIBGEN_DOMAINS:
        try:
            search_url = f"https://{domain}/index.php"
            # ads=false keeps the results table clean of injected ad rows
            resp = session.get(search_url, params={"req": query, "ads": "false"}, timeout=15, verify=False)

            if resp.status_code != 200:
                continue

            # If Cloudflare intercepts, skip domain
            if "cloudflare" in resp.text.lower() or "just a moment" in resp.text.lower():
                continue

            books = _parse_libgen_rows(resp.text, domain)
            if format_filter:
                books = [b for b in books if format_filter.lower() in b.get_available_formats()]

            # THE LOGICAL FIX: Connection worked, Cloudflare passed, but no rows parsed = 0 results
            if not books:
                return []

            return books
        except Exception as e:
            LOGGER.debug(f"[LibGen] {domain} failed: {e}")
            continue

    raise Exception("Could not reach active library networks. They may be temporarily down.")

def get_libgen_download_url(md5: str) -> Optional[str]:
    """Resolve the direct get.php download URL from a mirror's ads.php page."""
    for domain in LIBGEN_DOMAINS:
        try:
            resp = session.get(f"https://{domain}/ads.php", params={"md5": md5}, timeout=15, verify=False)
            if resp.status_code != 200:
                continue
            # The download key is generated fresh on every ads.php page load
            match = re.search(r'href="(get\.php\?md5=[a-f0-9]+&key=[^"]+)"', resp.text, re.IGNORECASE)
            if match:
                return f"https://{domain}/{match.group(1)}"
        except Exception as e:
            LOGGER.debug(f"[LibGen] ads.php failed on {domain}: {e}")
            continue
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
            status_msg.edit_text("😕 No results found. Try different keywords or check spelling.")
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
                f"[Try Manual Download](https://{book.domain}/ads.php?md5={book.md5})",
                parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
            )
            return

        status_msg.edit_text(f"⬇️ Downloading file to server...")

        try:
            # verify=False prevents download stream crashes on expired CDN certs
            resp = session.get(download_url, stream=True, timeout=60, verify=False)
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
            username = getattr(bot, "username", None) or "the bot"
            credit = f"@{username}" if username != "the bot" else "the bot"

            bot.send_document(
                chat_id=query.message.chat_id,
                document=file_obj,
                filename=filename,
                caption=f"📚 *{book.title}*\n✍️ *Author:* {book.author}\n📄 *Format:* {selected_format.upper()}\n\n_Downloaded Via {credit}_",
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

AABOOK_HANDLER = DisableAbleCommandHandler("aabook", aabook_command, pass_args=True)
AABOOK_CB_HANDLER = CallbackQueryHandler(aabook_callback, pattern=r"^aa_")

dispatcher.add_handler(BOOKINFO_HANDLER)
dispatcher.add_handler(BOOKINFO_CB_HANDLER)
dispatcher.add_handler(BOOK_HANDLER)
dispatcher.add_handler(OPENLIB_CB_HANDLER)
dispatcher.add_handler(ABOOK_HANDLER)
dispatcher.add_handler(ABOOK_MENU_HANDLER)
dispatcher.add_handler(ABOOK_DL_HANDLER)
dispatcher.add_handler(AABOOK_HANDLER)
dispatcher.add_handler(AABOOK_CB_HANDLER)

__help__ = """
📚 *Book Hub*

*Smart Recommendations:*
- `/bookinfo <query>`: Semantic search for book summaries and recommendations (e.g., "books about time travel").

*Library Search:*
- `/abook <title>`: Search and download books directly from Library Genesis.
- `/abook <title> --format epub`: Filter specific formats.

*Mega Search:*
- `/aabook <title>`: Search Anna's Archive (LibGen + Z-Library + Internet Archive + more) and download.

*Public Domain Library:*
- `/book <title>`: Download public domain books & classics safely.
"""

__mod_name__ = "Books"

LOGGER.info("Books module loaded successfully (LibGen + BookInfo Engine Active)!")
