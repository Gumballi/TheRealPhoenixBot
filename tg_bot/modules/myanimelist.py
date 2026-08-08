from typing import List
import requests

from telegram import Bot, Update, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import run_async
from tg_bot import OWNER_ID, MAL_CLIENT_ID, MAL_CLIENT_SECRET, MAL_ACCESS_TOKEN, MAL_REFRESH_TOKEN, dispatcher
from tg_bot.modules.disable import DisableAbleCommandHandler

# Import SQL database session and our helper model
from tg_bot.modules.sql import mal_sql as sql

MAL_API = "https://api.myanimelist.net/v2"

# Load tokens from database if they exist; otherwise, use env variables
db_tokens = sql.get_tokens()
if db_tokens:
    CURRENT_ACCESS_TOKEN = db_tokens.access_token
    CURRENT_REFRESH_TOKEN = db_tokens.refresh_token
else:
    CURRENT_ACCESS_TOKEN = MAL_ACCESS_TOKEN
    CURRENT_REFRESH_TOKEN = MAL_REFRESH_TOKEN
    # Initialize DB with env variables
    sql.update_tokens(CURRENT_ACCESS_TOKEN, CURRENT_REFRESH_TOKEN)


def _current_token():
    latest_tokens = sql.get_tokens()
    return (latest_tokens.access_token if latest_tokens and latest_tokens.access_token
            else CURRENT_ACCESS_TOKEN)


def _search(endpoint, query):
    url = f"{MAL_API}/{endpoint}?q={requests.utils.quote(query)}&limit=1"
    headers = {"Authorization": f"Bearer {_current_token()}"}
    req = requests.get(url, headers=headers)
    req.raise_for_status()
    items = req.json().get("data", [])
    return items[0]["node"] if items else None


def refresh_token(msg: Message, error: Exception) -> None:
    # Handle both malclient APIException and general HTTP 401 errors
    is_unauthorized = False
    if isinstance(error, requests.HTTPError) and error.response is not None and error.response.status_code == 401:
        is_unauthorized = True
    elif isinstance(error, requests.RequestException):
        is_unauthorized = False

    if is_unauthorized:
        # Get latest refresh token from DB
        latest_tokens = sql.get_tokens()
        r_token = latest_tokens.refresh_token if latest_tokens else MAL_REFRESH_TOKEN

        try:
            resp = requests.post(
                "https://myanimelist.net/v1/oauth2/token",
                data={
                    "client_id": MAL_CLIENT_ID,
                    "client_secret": MAL_CLIENT_SECRET,
                    "grant_type": "refresh_token",
                    "refresh_token": r_token,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            new_access_token = data.get("access_token", "")
            new_refresh_token = data.get("refresh_token", r_token)

            # Save new tokens directly to the SQL Database!
            sql.update_tokens(new_access_token, new_refresh_token)

            # Update local global-like state if necessary (though the DB is our main source of truth now)
            global CURRENT_ACCESS_TOKEN
            CURRENT_ACCESS_TOKEN = new_access_token

            MSG_TEXT = (f"*MAL tokens refreshed and saved to Database!*\n\n"
                        f"*New Access Token*: `{new_access_token[:15]}...`\n"
                        f"*New Refresh Token*: `{new_refresh_token[:15]}...`")
            dispatcher.bot.send_message(OWNER_ID, MSG_TEXT, parse_mode="MARKDOWN")

            msg.reply_text("Tokens refreshed successfully! Please try your search again.")
        except Exception as refresh_error:
            msg.reply_text(f"Failed to refresh MAL token: `{refresh_error}`", parse_mode="MARKDOWN")
    else:
        msg.reply_text(f"An error occurred:\n`{error}`", parse_mode="MARKDOWN")


@run_async
def search_anime(bot: Bot, update: Update, args: List[str]) -> None:
    msg = update.effective_message
    query = " ".join(args)
    if not query:
        msg.reply_text("I can't search for nothing...")
        return
    try:
        anime = _search("anime", query)
    except requests.HTTPError as e:
        refresh_token(msg, e)
        return
    except Exception as e:
        msg.reply_text(f"Failed to search anime: `{e}`", parse_mode="MARKDOWN")
        return
    if not anime:
        msg.reply_text("Not found!")
        return

    anime_id = anime.get("id")

    # Fetch details directly from MAL API using requests to bypass malclient validation & signature issues
    detail_fields = (
        "id,title,main_picture,alternative_titles,start_date,end_date,synopsis,mean,rank,"
        "popularity,num_list_users,num_scoring_users,nsfw,media_type,status,genres,"
        "num_episodes,start_season,source,studios"
    )
    
    # Always fetch the token dynamically from DB to ensure it's fresh
    latest_tokens = sql.get_tokens()
    token = latest_tokens.access_token if latest_tokens else CURRENT_ACCESS_TOKEN
    
    try:
        headers = {"Authorization": f"Bearer {token}"}
        url = f"https://api.myanimelist.net/v2/anime/{anime_id}?fields={detail_fields}"
        req = requests.get(url, headers=headers)
        req.raise_for_status()
        res = req.json()
    except requests.HTTPError as e:
        refresh_token(msg, e)
        return
    except Exception as e:
        msg.reply_text(f"Failed to fetch details: {e}")
        return
    
    raw_status = res.get("status", "")
    if raw_status == "finished_airing":
        status = "Finished Airing"
        episodes = res.get("num_episodes")
    else:
        episodes = None
        status = "Currently Airing" if raw_status == "currently_airing" else raw_status.replace('_', ' ').capitalize()
        
    genres_list = [i.get("name") for i in res.get("genres", [])] if res.get("genres") else []
    genres = ", ".join(genres_list) if genres_list else "None"
    
    studio_list = [i.get("name") for i in res.get("studios", [])] if res.get("studios") else []
    studios = ", ".join(studio_list) if studio_list else "None"
    
    premiered = "Unknown"
    start_season = res.get("start_season")
    if start_season:
        year = start_season.get("year", "")
        season = start_season.get("season", "").capitalize()
        premiered = f"{year} {season}".strip() or "Unknown"
        
    main_picture = res.get("main_picture", {})
    image = main_picture.get("large") or main_picture.get("medium") if main_picture else ""
    
    alt_titles = res.get("alternative_titles", {})
    ja_title = alt_titles.get("ja", "") if alt_titles else ""
    title_suffix = f" ({ja_title})" if ja_title else ""
    
    text = f"<b>{res.get('title')}{title_suffix}</b>\n"
    text += f"<b>Type</b>: <code>{res.get('media_type', 'Unknown').upper()}</code>\n"
    text += f"<b>Source</b>: <code>{res.get('source', 'Unknown').replace('_', ' ').capitalize()}</code>\n"
    text += f"<b>Status</b>: <code>{status}</code>\n"
    text += f"<b>Genres</b>: <code>{genres}</code>\n"
    if episodes:
        text += f"<b>Episodes</b>: <code>{episodes}</code>\n"
    text += f"<b>Score</b>: <code>{res.get('mean', 'N/A')}</code>\n"
    text += f"<b>Ranked</b>: <code>#{res.get('rank', 'N/A')}</code>\n"
    text += f"<b>Studio(s)</b>: <code>{studios}</code>\n"
    text += f"<b>Premiered</b>: <code>{premiered}</code>\n\n"
    if image:
        text += f"<a href='{image}'>\u200c</a>"
    text += res.get("synopsis", "")
    
    keyb = [
        [InlineKeyboardButton("More Information", url=f"https://myanimelist.net/anime/{anime_id}")]
    ]
    
    msg.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyb))


@run_async
def search_manga(bot: Bot, update: Update, args: List[str]) -> None:
    msg = update.effective_message
    query = " ".join(args)
    if not query:
        msg.reply_text("I can't search for nothing...")
        return
    try:
        manga = _search("manga", query)
    except requests.HTTPError as e:
        refresh_token(msg, e)
        return
    except Exception as e:
        msg.reply_text(f"Failed to search manga: `{e}`", parse_mode="MARKDOWN")
        return
    if not manga:
        msg.reply_text("Not found!")
        return

    manga_id = manga.get("id")

    # Fetch details directly from MAL API using requests
    manga_detail_fields = (
        "id,title,main_picture,alternative_titles,start_date,end_date,synopsis,mean,rank,"
        "popularity,num_list_users,num_scoring_users,nsfw,media_type,status,genres,"
        "num_volumes,num_chapters,authors"
    )
    
    latest_tokens = sql.get_tokens()
    token = latest_tokens.access_token if latest_tokens else CURRENT_ACCESS_TOKEN
    
    try:
        headers = {"Authorization": f"Bearer {token}"}
        url = f"https://api.myanimelist.net/v2/manga/{manga_id}?fields={manga_detail_fields}"
        req = requests.get(url, headers=headers)
        req.raise_for_status()
        res = req.json()
    except requests.HTTPError as e:
        refresh_token(msg, e)
        return
    except Exception as e:
        msg.reply_text(f"Failed to fetch details: {e}")
        return
    
    genres_list = [i.get("name") for i in res.get("genres", [])] if res.get("genres") else []
    genres = ", ".join(genres_list) if genres_list else "None"
    
    main_picture = res.get("main_picture", {})
    image = main_picture.get("large") or main_picture.get("medium") if main_picture else ""
    
    alt_titles = res.get("alternative_titles", {})
    ja_title = alt_titles.get("ja", "") if alt_titles else ""
    title_suffix = f" ({ja_title})" if ja_title else ""
    
    text = f"<b>{res.get('title')}{title_suffix}</b>\n"
    text += f"<b>Type</b>: <code>{res.get('media_type', 'Unknown').capitalize()}</code>\n"
    text += f"<b>Status</b>: <code>{res.get('status', 'Unknown').replace('_', ' ').capitalize()}</code>\n"
    text += f"<b>Genres</b>: <code>{genres}</code>\n"
    text += f"<b>Score</b>: <code>{res.get('mean', 'N/A')}</code>\n"
    text += f"<b>Ranked</b>: <code>#{res.get('rank', 'N/A')}</code>\n"
    if res.get("num_volumes"):
        text += f"<b>Volumes</b>: <code>{res.get('num_volumes')}</code>\n"
    if res.get("num_chapters"):
        text += f"<b>Chapters</b>: <code>{res.get('num_chapters')}</code>\n"
    if image:
        text += f"<a href='{image}'>\u200c</a>"
    text += f"\n{res.get('synopsis', '')}"
    
    keyb = [
        [InlineKeyboardButton("More Information", url=f"https://myanimelist.net/manga/{manga_id}")]
    ]
    
    msg.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyb))


__help__ = """
Get information about anime and manga with the help of this module! All data is fetched from [MyAnimeList](https://myanimelist.net).
*Available commands:*
 - /anime <anime>: returns information about the anime.
 - /manga <manga>: returns information about the manga.
 """

__mod_name__ = "MyAnimeList"

ANIME_HANDLER = DisableAbleCommandHandler("anime", search_anime, pass_args=True)
MANGA_HANDLER = DisableAbleCommandHandler("manga", search_manga, pass_args=True)

dispatcher.add_handler(ANIME_HANDLER)
dispatcher.add_handler(MANGA_HANDLER)
