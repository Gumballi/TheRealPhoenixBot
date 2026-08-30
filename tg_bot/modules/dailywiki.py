import os
import random
import logging

import requests
from telegram import Bot, Update, ParseMode
from telegram.ext import CommandHandler, run_async

from tg_bot import dispatcher, updater, OWNER_ID
from tg_bot.modules.helper_funcs.chat_status import user_admin
from tg_bot.modules.sql import dailywiki_sql as sql

LOGGER = logging.getLogger(__name__)

# Category -> list of Wikipedia categories to draw from randomly.
# Each type maps to a few top-level categories so posts stay on-theme.
CATEGORIES = {
    "science": ["Category:Science", "Category:Nature", "Category:Biology", "Category:Physics"],
    "tech_space": ["Category:Technology", "Category:Spaceflight", "Category:Astronomy", "Category:Computing"],
    "fun": ["Category:Trivia", "Category:Culture", "Category:Food"],
    "educational": ["Category:History", "Category:Geography", "Category:Mathematics", "Category:Language"],
}

# Daily rotation order, one type per day.
ROTATION = ["science", "tech_space", "fun", "educational"]

API = "https://en.wikipedia.org/w/api.php"
REST = "https://en.wikipedia.org/api/rest_v1"

# override from env so the owner can point elsewhere if needed
EN_WIKI = os.environ.get("WIKI_API", API)

# Wikipedia requires a descriptive User-Agent or it returns HTTP 403.
HEADERS = {"User-Agent": "PhoenixBot/1.0 (Telegram group bot; contact: owner)"}


def _get_random_title_in_category(category):
    """Return a random article title from a Wikipedia category."""
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": category,
        "cmtype": "page",
        "cmlimit": "max",
        "format": "json",
        "cmnamespace": "0",
    }
    resp = requests.get(EN_WIKI, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    members = data.get("query", {}).get("categorymembers", [])

    if not members:
        return None

    # shuffle to avoid always picking the first page
    random.shuffle(members)
    # sample up to 30 and pick a random one that has a summary
    pool = members[:30]
    random.shuffle(pool)
    for member in pool:
        title = member.get("title")
        if title:
            summary = _get_summary(title)
            if summary:
                return title, summary
    return None


def _get_summary(title):
    """Fetch the summary (extract) for a given article title."""
    url = f"{REST}/page/summary/{requests.utils.quote(title)}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        extract = data.get("extract") or ""
        if not extract:
            return None
        # trim to a reasonable length for a chat post
        if len(extract) > 1200:
            extract = extract[:1200].rsplit(" ", 1)[0] + "…"
        return {
            "title": data.get("title", title),
            "extract": extract,
            "url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
            "image": (data.get("thumbnail") or {}).get("source", ""),
        }
    except Exception as e:
        LOGGER.warning("[wiki] summary fetch failed for %s: %s", title, e)
        return None


def _build_post(day_index):
    """Build a single wiki post for the given day index. Returns None on failure."""
    cat_type = ROTATION[day_index % len(ROTATION)]
    categories = CATEGORIES[cat_type]
    random.shuffle(categories)

    result = None
    for category in categories:
        try:
            got = _get_random_title_in_category(category)
            if got:
                title, summary = got
                result = {
                    "type": cat_type,
                    "title": summary["title"],
                    "extract": summary["extract"],
                    "url": summary["url"],
                }
                break
        except Exception as e:
            LOGGER.warning("[wiki] category %s failed: %s", category, e)

    if not result:
        return None

    emoji_headers = {
        "science": "🔬 Science",
        "tech_space": "🚀 Tech & Space",
        "fun": "🎉 Fun Fact",
        "educational": "📚 Learn Something",
    }

    from telegram.utils.helpers import escape_markdown
    title = escape_markdown(result["title"], version=1)
    extract = escape_markdown(result["extract"], version=1)

    text = (f"*{emoji_headers[result['type']]} — {title}*\n\n"
            f"{extract}\n\n"
            f"[Read more]({result['url']})")
    return text


def _day_counter():
    """Return an integer that increments each day so the category rotates daily."""
    import datetime
    return datetime.date.today().toordinal()


def send_daily_wiki(bot: Bot, job):
    day_index = _day_counter()
    post = _build_post(day_index)
    if not post:
        LOGGER.warning("[wiki] no post could be built today, skipping")
        return

    for chat_id in sql.get_all_chats():
        try:
            bot.send_message(chat_id, post, parse_mode=ParseMode.MARKDOWN,
                             disable_web_page_preview=False)
        except Exception as e:
            LOGGER.warning("[wiki] failed to send to %s: %s", chat_id, e)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@run_async
def wiki(bot: Bot, update: Update):
    """Manually trigger a wiki post in the current chat."""
    message = update.effective_message
    day_index = _day_counter()
    post = _build_post(day_index)
    if not post:
        message.reply_text("Couldn't fetch a wiki article right now, try again later.")
        return
    message.reply_text(post, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=False)


@run_async
@user_admin
def set_wiki(bot: Bot, update: Update):
    """Set/update the daily wiki schedule for a chat. Usage: /setwiki HH:MM"""
    chat = update.effective_chat
    message = update.effective_message
    args = message.text.split(None, 1)

    time_val = "12:00"
    if len(args) > 1:
        time_val = args[1].strip()
        # basic validation HH:MM or H:MM
        import re
        if not re.match(r"^\d{1,2}:\d{2}$", time_val):
            message.reply_text("Invalid time. Use 24h format, e.g. `/setwiki 09:30`",
                               parse_mode=ParseMode.MARKDOWN)
            return

    sql.set_chat(chat.id, time_val)
    _restart_daily_job()
    message.reply_text(f"Daily wiki post enabled for this chat at `{time_val}`.",
                       parse_mode=ParseMode.MARKDOWN)


@run_async
@user_admin
def stop_wiki(bot: Bot, update: Update):
    """Stop the daily wiki schedule for a chat."""
    chat = update.effective_chat
    message = update.effective_message
    if sql.rem_chat(chat.id):
        _restart_daily_job()
        message.reply_text("Daily wiki post disabled for this chat.")
    else:
        message.reply_text("Daily wiki isn't enabled here.")


def _restart_daily_job():
    """Recreate the daily job with the earliest configured time."""
    chat_times = [sql.get_time(cid) for cid in sql.get_all_chats() if sql.get_time(cid)]

    job_queue = updater.job_queue
    for existing in list(job_queue.jobs()):
        if existing.name == "daily_wiki":
            existing.schedule_removal()

    if not chat_times:
        return

    import datetime
    earliest = min(chat_times)
    hour, minute = (int(x) for x in earliest.split(":"))
    job_queue.run_daily(send_daily_wiki, time=datetime.time(hour, minute), name="daily_wiki")


WIKI_HANDLER = CommandHandler("wiki", wiki)
SET_WIKI_HANDLER = CommandHandler("setwiki", set_wiki, filters=None)
STOP_WIKI_HANDLER = CommandHandler("stopwiki", stop_wiki)

dispatcher.add_handler(WIKI_HANDLER)
dispatcher.add_handler(SET_WIKI_HANDLER)
dispatcher.add_handler(STOP_WIKI_HANDLER)

_restart_daily_job()

__mod_name__ = "Daily Wiki"


def __stats__():
    return "{} chats with daily wiki enabled.".format(sql.num_chats())


def __migrate__(old_chat_id, new_chat_id):
    sql.migrate_chat(old_chat_id, new_chat_id)


__help__ = """
Every day the bot posts an educational or fun Wikipedia article to the group.

*Commands:*
• `/wiki`: manually post a wiki article in this chat.

*Admin only:*
• `/setwiki HH:MM`: enable daily wiki posts at this time (24h).
• `/stopwiki`: disable daily wiki posts.
"""
