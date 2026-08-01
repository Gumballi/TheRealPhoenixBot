import os
import io
import time
import zipfile
import requests
from bs4 import BeautifulSoup
import logging
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, ParseMode
from telegram.ext import run_async, CallbackQueryHandler

from tg_bot import dispatcher
from tg_bot.modules.disable import DisableAbleCommandHandler

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OMDB_API_KEY = os.environ.get("OMDB_API_KEY")
OMDB_URL = "http://www.omdbapi.com/"

# You can change this to a proxy (like yifysubtitles.ch) if .org ever blocks
YIFY_URL = "https://yifysubtitles.org" 
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
}

MAX_RESULTS = 3
RATE_LIMIT_SECONDS = 10       # per-user cooldown on /sub

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
# Search results keyed by the status message id. 
SEARCH_RESULTS = {}       # {msg_id: [movie_dict, ...]}
LAST_REQUEST = {}         # {user_id: timestamp}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _rate_limited(user_id):
    now = time.time()
    last = LAST_REQUEST.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    LAST_REQUEST[user_id] = now
    return False

# ---------------------------------------------------------------------------
# /sub command — search + show top matches for confirmation
# ---------------------------------------------------------------------------
@run_async
def search_subtitles(bot: Bot, update: Update, args):
    msg = update.effective_message
    user_id = update.effective_user.id

    if not args:
        msg.reply_text("Please provide a movie name! Example: `/sub Inception`", parse_mode=ParseMode.MARKDOWN)
        return

    if _rate_limited(user_id):
        msg.reply_text(f"Please wait a few seconds before searching again.")
        return

    if not OMDB_API_KEY:
        msg.reply_text("The bot owner has not configured the `OMDB_API_KEY`.", parse_mode=ParseMode.MARKDOWN)
        return

    movie_name = " ".join(args)
    status_msg = msg.reply_text(f"Searching for *{movie_name}*...", parse_mode=ParseMode.MARKDOWN)

    try:
        # Ask OMDb for the movie instead of YTS
        resp = requests.get(f"{OMDB_URL}?s={movie_name}&type=movie&apikey={OMDB_API_KEY}", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
    except requests.Timeout:
        status_msg.edit_text("The movie database took too long to respond. Try again shortly.")
        return
    except requests.RequestException as e:
        LOGGER.error(f"[Subtitles] OMDb API unreachable: {e}")
        status_msg.edit_text("Couldn't reach the movie database right now.")
        return
    except ValueError:
        LOGGER.error("[Subtitles] OMDb API returned non-JSON response")
        status_msg.edit_text("Movie database returned an unexpected response.")
        return

    # OMDb returns "Response": "False" if nothing is found
    if data.get("Response") == "False":
        status_msg.edit_text(f"No results found for *{movie_name}*. Check your spelling!", parse_mode=ParseMode.MARKDOWN)
        return

    # Extract the top results and format them to match our existing code
    movies = data.get("Search", [])
    top_matches = []
    
    for m in movies[:MAX_RESULTS]:
        top_matches.append({
            "title": m["Title"],
            "year": m["Year"],
            "imdb_code": m["imdbID"]
        })

    SEARCH_RESULTS[status_msg.message_id] = top_matches

    keyboard = [
        [InlineKeyboardButton(f"{m['title']} ({m['year']})", callback_data=f"mv_{status_msg.message_id}_{i}")]
        for i, m in enumerate(top_matches)
    ]
    status_msg.edit_text(
        f"🎬 Found {len(top_matches)} result(s) for *{movie_name}* — pick the right one:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )


# ---------------------------------------------------------------------------
# Movie selection — Scrape available languages and show dynamic buttons
# ---------------------------------------------------------------------------
@run_async
def movie_callback(bot: Bot, update: Update):
    query = update.callback_query
    _, msg_id_str, idx_str = query.data.split("_", 2)
    msg_id, idx = int(msg_id_str), int(idx_str)

    movies = SEARCH_RESULTS.get(msg_id)
    if not movies or idx >= len(movies):
        query.answer("This search has expired, please run /sub again.", show_alert=True)
        return

    movie = movies[idx]
    imdb_code = movie["imdb_code"]

    query.answer("Fetching available languages...")
    query.message.edit_text(f"🔍 Checking available languages for *{movie['title']}*...", parse_mode=ParseMode.MARKDOWN)

    # Scrape YIFY for this specific movie
    try:
        url = f"{YIFY_URL}/movie-imdb/{imdb_code}"
        html_resp = requests.get(url, headers=HEADERS, timeout=10)
        
        # If the movie doesn't have a page on YIFY at all
        if html_resp.status_code == 404:
            query.message.edit_text(f"No subtitles exist on YIFY for *{movie['title']}*.", parse_mode=ParseMode.MARKDOWN)
            return
            
        html_resp.raise_for_status()
        soup = BeautifulSoup(html_resp.text, 'html.parser')

        # Extract unique languages and their first/top download link
        available_subs = [] # List of tuples: (lang_name, zip_url)
        seen_langs = set()

        for a_tag in soup.find_all('a', href=True):
            if '/subtitles/' not in a_tag['href']:
                continue
                
            row = a_tag.find_parent('tr')
            if not row:
                continue

            lang_cell = row.find('td', class_='flag-cell') or row.find('span', class_='sub-lang')
            if not lang_cell:
                continue

            lang_name = lang_cell.get_text(strip=True)

            # We only keep the first link we find for each language (usually the most popular)
            if lang_name not in seen_langs:
                seen_langs.add(lang_name)
                dl_link = a_tag['href'].replace('/subtitles/', '/subtitle/') + '.zip'
                available_subs.append((lang_name, dl_link))

        if not available_subs:
            query.message.edit_text(f"No subtitles found for *{movie['title']}*.", parse_mode=ParseMode.MARKDOWN)
            return

        # Sort the languages alphabetically for a clean UI
        available_subs.sort(key=lambda x: x[0])
        
        # Save the scraped languages directly into our memory state
        movie['available_subs'] = available_subs

        # Build dynamic buttons (2 per row)
        keyboard = []
        row_btns = []
        for lang_idx, (lang_name, _) in enumerate(available_subs):
            btn = InlineKeyboardButton(lang_name, callback_data=f"lg_{msg_id}_{idx}_{lang_idx}")
            row_btns.append(btn)
            
            if len(row_btns) == 2:
                keyboard.append(row_btns)
                row_btns = []
                
        # Catch any leftover odd-numbered button
        if row_btns:
            keyboard.append(row_btns)

        query.message.edit_text(
            f"🎬 *{movie['title']} ({movie['year']})*\n\nSelect a subtitle language:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

    except requests.RequestException as e:
        LOGGER.error(f"[Subtitles] YIFY unreachable: {e}")
        query.message.edit_text("Couldn't reach the subtitle site right now.")


# ---------------------------------------------------------------------------
# Language selection — download and send the subtitle file
# ---------------------------------------------------------------------------
@run_async
def language_callback(bot: Bot, update: Update):
    query = update.callback_query
    _, msg_id_str, idx_str, lang_idx_str = query.data.split("_", 3)
    msg_id, idx, lang_idx = int(msg_id_str), int(idx_str), int(lang_idx_str)

    movies = SEARCH_RESULTS.get(msg_id)
    if not movies or idx >= len(movies):
        query.answer("This search has expired, please run /sub again.", show_alert=True)
        return

    movie = movies[idx]
    available_subs = movie.get('available_subs')
    
    if not available_subs or lang_idx >= len(available_subs):
        query.answer("Invalid language selection.", show_alert=True)
        return

    # Grab the pre-scraped URL from memory
    target_lang, subtitle_url = available_subs[lang_idx]

    query.answer(f"Downloading {target_lang} subtitles...")
    query.message.edit_text(f"Downloading {target_lang} subtitles for *{movie['title']}*...", parse_mode=ParseMode.MARKDOWN)

    try:
        zip_resp = requests.get(f"{YIFY_URL}{subtitle_url}", headers=HEADERS, timeout=10)
        zip_resp.raise_for_status()
    except requests.RequestException as e:
        LOGGER.error(f"[Subtitles] Failed to download zip: {e}")
        query.message.edit_text("Failed to download the subtitle archive. Try again shortly.")
        return

    try:
        with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as z:
            srt_files = [name for name in z.namelist() if name.endswith('.srt')]

            if not srt_files:
                query.message.edit_text("The subtitle archive didn't contain an .srt file.")
                return

            query.message.edit_text("Uploading subtitle file(s) to Telegram...")

            for srt_filename in srt_files:
                srt_content = z.read(srt_filename)
                bio = io.BytesIO(srt_content)
                bio.name = srt_filename
                bot.send_document(
                    chat_id=query.message.chat_id,
                    document=bio,
                    filename=srt_filename,
                    caption=f"{target_lang} subtitles — {movie['title']} ({movie['year']})"
                )

        query.message.delete()

    except zipfile.BadZipFile:
        LOGGER.error("[Subtitles] Downloaded file was not a valid zip")
        query.message.edit_text("The subtitle archive was corrupted or invalid.")
    except Exception as e:
        LOGGER.error(f"[Subtitles] Unexpected error extracting/sending subtitles: {e}")
        query.message.edit_text("An unexpected error occurred while processing the subtitle file.")


__help__ = """
 - /sub <movie name>: Search and download movie subtitles dynamically.
"""

__mod_name__ = "Subtitles"

SUB_HANDLER = DisableAbleCommandHandler("sub", search_subtitles, pass_args=True)
MOVIE_CALLBACK_HANDLER = CallbackQueryHandler(movie_callback, pattern=r"^mv_")
LANGUAGE_CALLBACK_HANDLER = CallbackQueryHandler(language_callback, pattern=r"^lg_")

dispatcher.add_handler(SUB_HANDLER)
dispatcher.add_handler(MOVIE_CALLBACK_HANDLER)
dispatcher.add_handler(LANGUAGE_CALLBACK_HANDLER)
