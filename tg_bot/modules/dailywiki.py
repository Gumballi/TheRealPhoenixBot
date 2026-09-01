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
    chat_id = job.context
    if str(chat_id) not in sql.get_all_chats():
        return
    day_index = _day_counter()
    post = _build_post(day_index)
    if not post:
        LOGGER.warning("[wiki] no post could be built for %s, skipping", chat_id)
        return
    try:
        bot.send_message(chat_id, post, parse_mode=ParseMode.MARKDOWN,
                         disable_web_page_preview=False)
        LOGGER.info("[wiki] posted daily wiki to chat %s", chat_id)
    except Exception as e:
        LOGGER.warning("[wiki] failed to send to %s: %s", chat_id, e)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_offset(raw):
    """Parse '+3', '-05:30', 'UTC+3', '+3:00' → minutes east of UTC. None if bad."""
    import re
    s = (raw or "").strip().upper().replace("UTC", "").replace("GMT", "").strip()
    if not s:
        return 0
    sign = 1
    if s[0] in "+-":
        if s[0] == "-":
            sign = -1
        s = s[1:]
    if not s or not re.match(r"^\d{1,2}(:\d{2})?$", s):
        return None
    parts = s.split(":")
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return None
    if hours > 23 or minutes > 59:
        return None
    return sign * (hours * 60 + minutes)


def _format_offset(offset_min):
    sign = "+" if offset_min >= 0 else "-"
    m = abs(offset_min)
    return "UTC{}{:02d}:{:02d}".format(sign, m // 60, m % 60)


def _local_to_utc(local_hhmm, offset_min):
    """Convert a local time-of-day (UTC+offset_min) to a UTC datetime.time."""
    import datetime
    hour, minute = (int(x) for x in local_hhmm.split(":"))
    local = datetime.datetime(2000, 1, 1, hour, minute)
    utc = local - datetime.timedelta(minutes=offset_min)
    return utc.time()


def _next_utc_datetime(utc_time):
    """Next datetime.datetime matching *utc_time* (today, or tomorrow if passed)."""
    import datetime
    now = datetime.datetime.now()
    when = datetime.datetime.combine(now.date(), utc_time)
    if when <= now:
        when += datetime.timedelta(days=1)
    return when


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@run_async
def wiki_today(bot: Bot, update: Update):
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
    """Set/update the daily wiki schedule for a chat. Usage: /setwiki HH:MM [UTC±HH:MM]"""
    import re
    import datetime
    chat = update.effective_chat
    message = update.effective_message
    args = message.text.split(None, 1)

    time_val = "12:00"
    offset_raw = "+0"
    if len(args) > 1:
        fields = args[1].strip().split()
        time_val = fields[0]
        time_match = re.match(r"^(\d{1,2}):(\d{2})$", time_val)
        if not time_match:
            message.reply_text("Invalid time. Use 24h format, e.g. `/setwiki 09:30`",
                               parse_mode=ParseMode.MARKDOWN)
            return
        hour, minute = int(time_match.group(1)), int(time_match.group(2))
        if hour > 23 or minute > 59:
            message.reply_text(
                "Invalid time. Hours must be 00-23 and minutes 00-59, e.g. `/setwiki 09:30`",
                parse_mode=ParseMode.MARKDOWN)
            return
        if len(fields) > 1:
            offset_raw = fields[1]

    offset_min = _parse_offset(offset_raw)
    if offset_min is None:
        message.reply_text(
            "Invalid UTC offset. Use e.g. `/setwiki 09:30 UTC+3` or `/setwiki 09:30 +05:30`.",
            parse_mode=ParseMode.MARKDOWN)
        return

    sql.set_chat(chat.id, time_val, str(offset_min))
    _restart_daily_job()

    utc_time = _local_to_utc(time_val, offset_min)
    next_fire = _next_utc_datetime(utc_time)
    message.reply_text(
        "Daily wiki posts enabled for this chat.\n"
        "• Local time: `{}` ({})\n"
        "• Next post: `{}` UTC".format(
            time_val, _format_offset(offset_min),
            next_fire.strftime("%b %d %H:%M")),
        parse_mode=ParseMode.MARKDOWN)


@run_async
@user_admin
def stop_wiki(bot: Bot, update: Update):
    """Stop the daily wiki schedule for a chat."""
    chat = update.effective_chat
    message = update.effective_message
    if sql.rem_chat(chat.id):
        _restart_daily_job()
        message.reply_text("Daily wiki posts disabled for this chat.")
    else:
        message.reply_text("Daily wiki isn't enabled here.")


def _restart_daily_job():
    """Rebuild one daily job per chat at that chat's UTC-converted time."""
    import datetime
    job_queue = updater.job_queue

    for existing in list(job_queue.jobs()):
        if existing.name and existing.name.startswith("daily_wiki_"):
            existing.schedule_removal()

    for chat_id in sql.get_all_chats():
        time_val = sql.get_time(chat_id)
        if not time_val:
            continue
        offset_min = sql.get_offset(chat_id)
        utc_time = _local_to_utc(time_val, offset_min)
        job_queue.run_daily(
            send_daily_wiki,
            time=utc_time,
            context=int(chat_id),
            name="daily_wiki_{}".format(chat_id))
        LOGGER.info("[wiki] scheduled daily post for chat %s at %s UTC", chat_id, utc_time)


WIKI_HANDLER = CommandHandler("wikitoday", wiki_today)
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
• `/wikitoday`: manually post a wiki article in this chat.

*Admin only:*
• `/setwiki HH:MM [UTC±HH:MM]`: enable daily wiki posts (24h time). Optionally give your
  UTC offset, e.g. `/setwiki 09:30 UTC+3` or `/setwiki 09:30 -05:30`. Without an offset the
  time is treated as UTC (the server timezone).
• `/stopwiki`: disable daily wiki posts.
"""
