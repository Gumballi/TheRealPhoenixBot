import os
import html
import random
import re
import json
import urllib.request
import urllib.parse
import wikipedia
import requests
import logging
import time
import sqlite3
import threading
from typing import List, Tuple
from wikipedia.exceptions import DisambiguationError, PageError

from telegram import Message, Chat, Update, Bot, ParseMode
from telegram.error import RetryAfter, BadRequest
from telegram.ext import run_async, MessageHandler, Filters

from tg_bot import dispatcher
from tg_bot.modules.disable import DisableAbleCommandHandler

LOGGER = logging.getLogger(__name__)

# Fetch API key securely from environment variables
NIGHT_API_KEY = os.environ.get("NIGHT_API_KEY")
NIGHT_API_URL = "https://api.night-api.com/images/nsfw"

# ==========================================
# TAG ALL / @ALL CACHE DATABASE SETUP
# ==========================================
TAG_DB_PATH = "tagall_cache.db"
TAG_DB_CONN = sqlite3.connect(TAG_DB_PATH, check_same_thread=False)
TAG_CURSOR = TAG_DB_CONN.cursor()

TAG_CURSOR.execute("""
    CREATE TABLE IF NOT EXISTS chat_users (
        chat_id INTEGER,
        user_id INTEGER,
        first_name TEXT,
        PRIMARY KEY (chat_id, user_id)
    )
""")
TAG_DB_CONN.commit()

TAGGING_STATE = {}

# ==========================================
# CONSTANTS & LISTS
# ==========================================
SHRUGS = (
    "┐(´д｀)┌",
    "┐(´～｀)┌",
    "┐(´ー｀)┌",
    "┐(￣ヘ￣)┌",
    "╮(╯∀╰)╭",
    "╮(╯_╰)╭",
    "┐(´д`)┌",
    "┐(´∀｀)┌",
    "ʅ(́◡◝)ʃ",
    "┐(ﾟ～ﾟ)┌",
    "┐('д')┌",
    "┐(‘～`;)┌",
    "ヘ(´－｀;)ヘ",
    "┐( -“-)┌",
    "ʅ（´◔౪◔）ʃ",
    "ヽ(゜～゜o)ノ",
    "ヽ(~～~ )ノ",
    "┐(~ー~;)┌",
    "┐(-。ー;)┌",
    r"¯\_(ツ)_/¯",
    r"¯\_(⊙_ʖ⊙)_/¯",
    r"¯\_༼ ಥ ‿ ಥ ༽_/¯",
    "乁( ⁰͡  Ĺ̯ ⁰͡ ) ㄏ",
)

HUGS = (
"⊂(・﹏・⊂)",
"⊂(・ヮ・⊂)",
"⊂(・▽・⊂)",
"(っಠ‿ಠ)っ",
"ʕっ•ᴥ•ʔっ",
"（っ・∀・）っ",
"(っ⇀⑃↼)っ",
"(つ´∀｀)つ",
"(.づσ▿σ)づ.",
"⊂(´・ω・｀⊂)",
"(づ￣ ³￣)づ",
"(.づ◡﹏◡)づ.",
)

TOSS = (
"The coin landed on heads.",
"The coin landed on tails."
)

REACTS = (
    "ʘ‿ʘ",
    "ヾ(-_- )ゞ",
    "(っ˘ڡ˘ς)",
    "(´ж｀ς)",
    "( ಠ ʖ̯ ಠ)",
    "(° ͜ʖ͡°)╭∩╮",
    "(ᵟຶ︵ ᵟຶ)",
    "(งツ)ว",
    "ʚ(•｀",
    "(っ▀¯▀)つ",
    "(◠﹏◠)",
    "( ͡ಠ ʖ̯ ͡ಠ)",
    "( ఠ ͟ʖ ఠ)",
    "(∩｀-´)⊃━☆ﾟ.*･｡ﾟ",
    "(⊃｡•́‿•̀｡)⊃",
    "(._.)",
    "{•̃_•̃}",
    "(ᵔᴥᵔ)",
    "♨_♨",
    "⥀.⥀",
    "ح˚௰˚づ ",
    "(҂◡_◡)",
    "ƪ(ړײ)‎ƪ​​",
    "(っ•́｡•́)♪♬",
    "◖ᵔᴥᵔ◗ ♪ ♫ ",
    "(☞ﾟヮﾟ)☞",
    "[¬º-°]¬",
    "(Ծ‸ Ծ)",
    "(•̀ᴗ•́)و ̑̑",
    "ヾ(´〇`)ﾉ♪♪♪",
    "(ง'̀-'́)ง",
    "ლ(•́•́ლ)",
    "ʕ •́؈•̀ ₎",
    "♪♪ ヽ(ˇ∀ˇ )ゞ",
    "щ（ﾟДﾟщ）",
    "( ˇ෴ˇ )",
    "눈_눈",
    "(๑•́ ₃ •̀๑) ",
    "( ˘ ³˘)♥ ",
    "ԅ(≖‿≖ԅ)",
    "♥‿♥",
    "◔_◔",
    "⁽⁽ଘ( ˊᵕˋ )ଓ⁾⁾",
    "乁( ◔ ౪◔)「     ┑(￣Д ￣)┍",
    "( ఠൠఠ )ﾉ",
    "٩(๏_๏)۶",
    "┌(ㆆ㉨ㆆ)ʃ",
    "ఠ_ఠ",
    "(づ｡◕‿‿◕｡)づ",
    "(ノಠ ∩ಠ)ノ彡( \\o°o)\\",
    "“ヽ(´▽｀)ノ”",
    "༼ ༎ຶ ෴ ༎ຶ༽",
    "｡ﾟ( ﾟஇ‸இﾟ)ﾟ｡",
    "(づ￣ ³￣)づ",
    "(⊙.☉)7",
    "ᕕ( ᐛ )ᕗ",
    "t(-_-t)",
    "(ಥ⌣ಥ)",
    "ヽ༼ ಠ益ಠ ༽ﾉ",
    "༼∵༽ ༼⍨༽ ༼⍢༽ ༼⍤༽",
    "ミ●﹏☉ミ",
    "(⊙_◎)",
    "¿ⓧ_ⓧﮌ",
    "ಠ_ಠ",
    "(´･_･`)",
    "ᕦ(ò_óˇ)ᕤ",
    "⊙﹏⊙",
    "(╯°□°）╯︵ ┻━┻",
    r"¯\_(⊙︿⊙)_/¯",
    "٩◔̯◔۶",
    "°‿‿°",
    "ᕙ(⇀‸↼‶)ᕗ",
    "⊂(◉‿◉)つ",
    "V•ᴥ•V",
    "q(❂‿❂)p",
    "ಥ_ಥ",
    "ฅ^•ﻌ•^ฅ",
    "ಥ﹏ಥ",
    "（ ^_^）o自自o（^_^ ）",
    "ಠ‿ಠ",
    "ヽ(´▽`)/",
    "ᵒᴥᵒ#",
    "( ͡° ͜ʖ ͡°)",
    "┬─┬﻿ ノ( ゜-゜ノ)",
    "ヽ(´ー｀)ノ",
    "☜(⌒▽⌒)☞",
    "ε=ε=ε=┌(;*´Д`)ﾉ",
    "(╬ ಠ益ಠ)",
    "┬─┬⃰͡ (ᵔᵕᵔ͜ )",
    "┻━┻ ︵ヽ(`Д´)ﾉ︵﻿ ┻━┻",
    "ʕᵔᴥᵔʔ",
    "(`･ω･´)",
    "ʕ•ᴥ•ʔ",
    "ლ(｀ー´ლ)",
    "ʕʘ̅͜ʘ̅ʔ",
    "（ ﾟДﾟ）",
    r"¯\(°_o)/¯",
    "(｡◕‿◕｡)",
)

normiefont = ['a','b','c','d','e','f','g','h','i','j','k','l','m','n','o','p','q','r','s','t','u','v','w','x','y','z']
weebyfont = ['卂','乃','匚','刀','乇','下','厶','卄','工','丁','长','乚','从','𠘨','口','尸','㔿','尺','丂','丅','凵','リ','山','乂','丫','乙']

# ==========================================
# EXTRAS HANDLERS
# ==========================================
@run_async
def shrug(bot: Bot, update: Update):
    # reply to correct message 
    reply_text = update.effective_message.reply_to_message.reply_text if update.effective_message.reply_to_message else update.effective_message.reply_text
    reply_text(random.choice(SHRUGS))

@run_async
def hug(bot: Bot, update: Update):
    # reply to correct message 
    reply_text = update.effective_message.reply_to_message.reply_text if update.effective_message.reply_to_message else update.effective_message.reply_text
    reply_text(random.choice(HUGS))
    
@run_async
def toss(bot: Bot, update: Update):
     update.effective_message.reply_text(random.choice(TOSS))

@run_async
def react(bot: Bot, update: Update):
     # reply to correct message 
    reply_text = update.effective_message.reply_to_message.reply_text if update.effective_message.reply_to_message else update.effective_message.reply_text
    reply_text(random.choice(REACTS))
    
@run_async
def shout(bot: Bot, update: Update, args):
    msg = "```"
    text = " ".join(args)
    result = []
    result.append(' '.join([s for s in text]))
    for pos, symbol in enumerate(text[1:]):
        result.append(symbol + ' ' + '  ' * pos + symbol)
    result = list("\n".join(result))
    result[0] = text[0]
    result = "".join(result)
    result = str(result).upper()
    msg = "```\n" + result + "```"
    return update.effective_message.reply_text(msg, parse_mode="MARKDOWN")

@run_async
def pat(bot: Bot, update: Update):
    chat_id = update.effective_chat.id
    msg = str(update.message.text)
    try:
        msg = msg.split(" ", 1)[1]
    except IndexError:
        msg = ""
    msg_id = update.effective_message.reply_to_message.message_id if update.effective_message.reply_to_message else update.effective_message.message_id
    pats = []
    pats = json.loads(urllib.request.urlopen(urllib.request.Request(
    '[http://headp.at/js/pats.json](http://headp.at/js/pats.json)',
    headers={'User-Agent': 'Mozilla/5.0 (X11; U; Linux i686) '
         'Gecko/20071127 Firefox/2.0.0.11'}
    )).read().decode('utf-8'))
    if "@" in msg and len(msg) > 5:
        bot.send_photo(chat_id, f'[https://headp.at/pats/](https://headp.at/pats/){urllib.parse.quote(random.choice(pats))}', caption=msg)
    else:
        bot.send_photo(chat_id, f'[https://headp.at/pats/](https://headp.at/pats/){urllib.parse.quote(random.choice(pats))}', reply_to_message_id=msg_id)

@run_async
def spank(bot: Bot, update: Update):
    chat_id = update.effective_chat.id
    msg = update.effective_message
    sender = update.effective_user.first_name
    
    # Identify target (either who we are replying to or who is mentioned)
    target = ""
    if msg.reply_to_message:
        target = msg.reply_to_message.from_user.first_name
    else:
        # Check for arguments/tags following /spank
        args = msg.text.split(" ", 1)
        if len(args) > 1:
            target = args[1].strip()

    try:
        req = urllib.request.Request(
            '[https://nekos.best/api/v2/slap](https://nekos.best/api/v2/slap)',
            headers={'User-Agent': 'TheRealPhoenixBot/1.0 ([https://github.com/Gumballi/TheRealPhoenixBot](https://github.com/Gumballi/TheRealPhoenixBot))'}
        )
        res = urllib.request.urlopen(req, timeout=8)
        if res.status != 200:
            msg.reply_text(f"Nekos.best API returned status {res.status}. Try again shortly!")
            return
        res_data = json.loads(res.read().decode('utf-8'))
        gif_url = res_data['results'][0]['url']
    except Exception as e:
        msg.reply_text("Failed to fetch a reaction GIF from the web API. Try again shortly!")
        return

    # Build dynamic message
    if target:
        caption = f"*{sender}* spanked *{target}*!"
    else:
        caption = f"*{sender}* is looking around for someone to spank..."

    # If replying, match the structure and target reply_to_message_id
    msg_id = msg.reply_to_message.message_id if msg.reply_to_message else msg.message_id
    
    bot.send_document(
        chat_id=chat_id,
        document=gif_url,
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_to_message_id=msg_id
    )

@run_async
def cuddle(bot: Bot, update: Update):
    chat_id = update.effective_chat.id
    msg = update.effective_message
    sender = update.effective_user.first_name

    # Identify target (either who we are replying to or who is mentioned)
    target = ""
    if msg.reply_to_message:
        target = msg.reply_to_message.from_user.first_name
    else:
        args = msg.text.split(" ", 1)
        if len(args) > 1:
            target = args[1].strip()

    # Call Nekos.best API to fetch a random cuddle GIF
    try:
        req = urllib.request.Request(
            '[https://nekos.best/api/v2/cuddle](https://nekos.best/api/v2/cuddle)',
            headers={'User-Agent': 'TheRealPhoenixBot/1.0 ([https://github.com/Gumballi/TheRealPhoenixBot](https://github.com/Gumballi/TheRealPhoenixBot))'}
        )
        res = urllib.request.urlopen(req, timeout=8)
        if res.status != 200:
            msg.reply_text(f"Nekos.best API returned status {res.status}. Try again shortly!")
            return
        res_data = json.loads(res.read().decode('utf-8'))
        gif_url = res_data['results'][0]['url']
    except Exception as e:
        msg.reply_text("Failed to fetch a cuddle GIF from the web API. Try again shortly!")
        return

    # Build dynamic message
    if target:
        caption = f"*{sender}* cuddled *{target}*!"
    else:
        caption = f"*{sender}* is looking around for someone to cuddle..."

    msg_id = msg.reply_to_message.message_id if msg.reply_to_message else msg.message_id

    bot.send_document(
        chat_id=chat_id,
        document=gif_url,
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_to_message_id=msg_id
    )

@run_async
def kiss(bot: Bot, update: Update):
    chat_id = update.effective_chat.id
    msg = update.effective_message
    sender = update.effective_user.first_name
    user = update.effective_user

    target_name = ""
    target_id = None
    is_bot = False
    is_self = False

    if msg.reply_to_message:
        target = msg.reply_to_message.from_user
        target_name = target.first_name
        target_id = target.id
        is_bot = target.id == bot.id
        is_self = target.id == user.id
    else:
        args = msg.text.split(" ", 1)
        if len(args) > 1:
            target_name = args[1].strip()
            if target_name.lower() == "@{}".format(bot.username.lower()):
                is_bot = True
            elif user.username and target_name.lower() == "@{}".format(user.username.lower()):
                is_self = True
        else:
            msg.reply_text("Reply to someone's message or tag them to kiss them.")
            return

    if is_bot:
        msg.reply_text("I am a bot. You cannot kiss me.")
        return

    if is_self:
        msg.reply_text("You cannot kiss yourself.")
        return

    try:
        api_url = "[https://api.gifukai.com/kiss?type=mouth&pairing=fm](https://api.gifukai.com/kiss?type=mouth&pairing=fm)" 
        
        req = requests.get(api_url, timeout=8)
        if req.status_code == 200:
            data = req.json()
            gif_url = data.get("url") 
            
            if not gif_url:
                msg.reply_text("The API returned an unexpected response.")
                return

            safe_user_html = html.escape(user.first_name)
            safe_target_html = html.escape(target_name)

            if target_id:
                caption = f"<b>{safe_user_html}</b> kissed <a href='tg://user?id={target_id}'>{safe_target_html}</a>!"
            else:
                caption = f"<b>{safe_user_html}</b> kissed {safe_target_html}!"
            
            msg_id = msg.reply_to_message.message_id if msg.reply_to_message else msg.message_id

            bot.send_animation(
                chat_id=chat_id,
                animation=gif_url,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=msg_id
            )
        else:
            msg.reply_text("The API is currently unresponsive.")
    except Exception as e:
        print(f"Kiss Command Error: {e}")
        msg.reply_text("An error occurred while fetching the animation.")

@run_async
def wiki(bot: Bot, update: Update):
    msg = update.effective_message.reply_to_message if update.effective_message.reply_to_message else update.effective_message
    res = ""
    
    # Bug 1 Fix: Safely handle parsing without crashing on empty /wiki commands
    if msg == update.effective_message:
        parts = msg.text.split(" ", maxsplit=1)
        if len(parts) < 2:
            update.effective_message.reply_text("Please provide a search term! Example: /wiki Python (programming language)")
            return
        search = parts[1]
    else:
        search = msg.text

    try:
        # Bug 2 Fix: Attempt to get the page summary
        res = wikipedia.summary(search, sentences=3) # Limit to 3 sentences to keep telegram clean
    except DisambiguationError as e:
        update.effective_message.reply_text(
            f"<b>Disambiguation found!</b> Adjust your query accordingly:\n\n<i>{e.options[:5]}</i>",
            parse_mode=ParseMode.HTML
        )
        return # Stop execution here
    except PageError as e:
        # If the page doesn't exist, try to search for similar suggestions
        suggestions = wikipedia.search(search)
        if suggestions:
            update.effective_message.reply_text(
                f"Page not found. Did you mean one of these?\n• <code>" + "</code>\n• <code>".join(suggestions[:5]) + "</code>", 
                parse_mode=ParseMode.HTML
            )
        else:
            update.effective_message.reply_text(f"Page not found for: <code>{search}</code>", parse_mode=ParseMode.HTML)
        return # Stop execution here
    except Exception as e:
        update.effective_message.reply_text(f"An unexpected error occurred: {str(e)}")
        return

    # Send result if we obtained a valid summary
    if res:
        result = f"<b>{search.title()}</b>\n\n"
        result += f"<i>{res}</i>\n\n"
        result += f"""<a href="[https://en.wikipedia.org/wiki/](https://en.wikipedia.org/wiki/){urllib.parse.quote(search)}">Read more...</a>"""
        
        if len(result) > 4000:
            with open("result.txt", 'w', encoding='utf-8') as f:
                f.write(result)
            with open("result.txt", 'rb') as f:
                bot.send_document(
                    document=f, 
                    filename="wiki_result.txt",
                    reply_to_message_id=update.effective_message.message_id, 
                    chat_id=update.effective_chat.id
                )
        else:
            update.effective_message.reply_text(result, parse_mode=ParseMode.HTML, disable_web_page_preview=False)

@run_async
def judge(bot: Bot, update: Update):
    judger = ["<b>is lying!</b>", "<b>is telling the truth!</b>"]
    rep = update.effective_message
    msg = ""
    msg = update.effective_message.reply_to_message
    if not msg:
        rep.reply_text("Reply to someone's message to judge them!")
    else:
        user = msg.from_user.first_name
    res = random.choice(judger)
    msg.reply_text(f"{user} {res}", parse_mode=ParseMode.HTML)

@run_async
def weebify(bot: Bot, update: Update, args):
    msg = update.effective_message
    if args:
        string = " ".join(args).lower()
    elif msg.reply_to_message:
        string = msg.reply_to_message.text.lower()
    else:
        msg.reply_text("Enter some text to weebify or reply to someone's message!")
        return
        
    for normiecharacter in string:
        if normiecharacter in normiefont:
            weebycharacter = weebyfont[normiefont.index(normiecharacter)]
            string = string.replace(normiecharacter, weebycharacter)

    if msg.reply_to_message:
        msg.reply_to_message.reply_text(string)
    else:
        msg.reply_text(string)

@run_async
def night_api_nsfw(bot: Bot, update: Update, args):
    msg = update.effective_message
    
    if not NIGHT_API_KEY:
        msg.reply_text("The bot owner has not configured the `NIGHT_API_KEY`.", parse_mode=ParseMode.MARKDOWN)
        return

    # Default to 'hentai' if the user doesn't provide a specific argument
    category = args[0].lower() if args else "hentai"
    
    # Common categories for Night API
    valid_categories = ["hentai", "boobs", "pussy", "ass", "feet"]
    
    if category not in valid_categories:
        msg.reply_text(f"Invalid category! Available options:\n`{', '.join(valid_categories)}`", parse_mode=ParseMode.MARKDOWN)
        return

    # Send a typing action since external API calls can take a second to resolve
    bot.send_chat_action(chat_id=msg.chat_id, action="upload_photo")

    headers = {
        "Authorization": f"{NIGHT_API_KEY}" 
    }
    
    try:
        response = requests.get(f"{NIGHT_API_URL}/{category}", headers=headers, timeout=10)
        
        # Check HTTP network status
        if response.status_code == 200:
            data = response.json()
            
            # Check internal API status
            api_status = data.get("status")
            if api_status == 400:
                error_msg = data.get("content", "Invalid request")
                msg.reply_text(f API Error: {error_msg}")
                return
            
            # Safely extract the image URL
            image_url = None
            content = data.get("content")
            
            if isinstance(content, dict):
                image_url = content.get("url")
            elif isinstance(content, str) and content.startswith("http"):
                image_url = content
            else:
                image_url = data.get("url") or data.get("message")
            
            if image_url:
                msg.reply_photo(photo=image_url)
            else:
                msg.reply_text("API request succeeded, but couldn't parse the image URL from the JSON.")
                
        elif response.status_code == 401:
            msg.reply_text("Unauthorized! The provided Night API key is invalid.")
        elif response.status_code == 404:
            msg.reply_text("Endpoint not found. Night API may have renamed this category.")
        else:
            msg.reply_text(f"HTTP Error: {response.status_code}")
            
    except requests.exceptions.RequestException as e:
        LOGGER.error(f"[Night-API] Request failed: {e}")
        msg.reply_text("An error occurred while communicating with the Night API servers.")

# ==========================================
# @ALL / TAG ALL LOGIC
# ==========================================
def add_user(chat_id: int, user_id: int, first_name: str):
    TAG_CURSOR.execute(
        "INSERT OR REPLACE INTO chat_users (chat_id, user_id, first_name) VALUES (?, ?, ?)",
        (chat_id, user_id, first_name)
    )
    TAG_DB_CONN.commit()

def get_users(chat_id: int) -> List[Tuple[int, str]]:
    TAG_CURSOR.execute("SELECT user_id, first_name FROM chat_users WHERE chat_id = ?", (chat_id,))
    return TAG_CURSOR.fetchall()

def is_user_admin(chat, user_id: int) -> bool:
    if chat.type == 'private':
        return True
    try:
        member = chat.get_member(user_id)
        return member.status in ('administrator', 'creator')
    except Exception:
        return False

def tag_worker(bot, chat_id: int, users: List[Tuple[int, str]], message_text: str):
    """Background thread to tag 5 users at a time with anti-spam delays."""
    chunk_size = 5
    for i in range(0, len(users), chunk_size):
        if not TAGGING_STATE.get(chat_id, False):
            break
            
        chunk = users[i:i + chunk_size]
        mentions = []
        
        for user_id, first_name in chunk:
            safe_name = first_name.replace('[', '').replace(']', '').replace('*', '').replace('_', '')
            if not safe_name.strip():
                safe_name = "User"
            mentions.append(f"[{safe_name}](tg://user?id={user_id})")
            
        tag_text = f"{message_text}\n\n" + ", ".join(mentions)
        
        try:
            bot.send_message(chat_id, tag_text, parse_mode=ParseMode.MARKDOWN)
            time.sleep(2.5)  # Strict delay to prevent Telegram from blocking the bot
            
        except RetryAfter as e:
            time.sleep(e.retry_after + 1)
        except BadRequest:
            pass
        except Exception:
            pass

    TAGGING_STATE[chat_id] = False
    try:
        bot.send_message(chat_id, "**Tagging process finished!**", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass

@run_async
def passive_tracker(bot, update: Update):
    """Silently logs active users."""
    if not update.effective_message or not update.effective_chat or not update.effective_user:
        return
        
    chat = update.effective_chat
    user = update.effective_user
    
    if chat.type == 'private' or user.is_bot:
        return
        
    add_user(chat.id, user.id, user.first_name)

@run_async
def tag_all(bot, update: Update, args: List[str] = None, regex_match=None):
    """Triggers the tagging process."""
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message

    if chat.type == 'private':
        msg.reply_text("This command can only be used in groups.")
        return

    if not is_user_admin(chat, user.id):
        msg.reply_text("Only group admins can tag everyone.")
        return

    if TAGGING_STATE.get(chat.id, False):
        msg.reply_text("A tagging process is already running! Use `/cancelall` to stop it.")
        return

    text = ""
    if args:
        text = " ".join(args)
    elif regex_match:
        text = regex_match.group(1).strip()

    if not text:
        text = "**Attention Everyone!**"

    users = get_users(chat.id)
    if not users:
        msg.reply_text("I don't have any users cached for this chat yet! People need to send messages first.")
        return

    TAGGING_STATE[chat.id] = True
    msg.reply_text(f"**Starting to tag {len(users)} users...**\n_Use /cancelall to stop._", parse_mode=ParseMode.MARKDOWN)

    threading.Thread(target=tag_worker, args=(bot, chat.id, users, text), daemon=True).start()

@run_async
def tag_all_regex(bot, update: Update):
    """Catches @all triggers."""
    msg_text = update.effective_message.text
    match = re.match(r"(?i)^@all(.*)", msg_text)
    if match:
        tag_all(bot, update, regex_match=match)

@run_async
def cancel_tag_all(bot, update: Update):
    """Aborts an active tag loop."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == 'private':
        return

    if not is_user_admin(chat, user.id):
        update.effective_message.reply_text("Only admins can cancel tagging.")
        return

    if not TAGGING_STATE.get(chat.id, False):
        update.effective_message.reply_text("There is no active tagging process to cancel.")
        return

    TAGGING_STATE[chat.id] = False
    update.effective_message.reply_text("**Stopping the tagging process...**", parse_mode=ParseMode.MARKDOWN)


# ==========================================
# HELP MENU & REGISTRATIONS
# ==========================================
__help__ = """
 - /shg or /shrug: pretty self-explanatory.
 - /hug: give a hug and spread the love :)
 - /pat: give a headpat :3
 - /spank: spank someone playfully!
 - /cuddle: cuddle someone!
 - /react: send a random reaction.
 - /toss: toss a coin.
 - /shout <word>: shout the specified word in the chat.
 - /wiki <term>: do a search on Wikipedia.
 - /judge: as a reply to someone, checks if they're lying or not!
 - /weebify: as a reply to a message, "weebifies" the message.
 - /nsfw <category>: Fetch a random NSFW image (defaults to hentai). Categories: hentai, boobs, pussy, ass, feet.
 
 *Tag All Commands:*
 - `@all <message>` or `/all <message>`: Tag everyone in the group.
 - `/cancelall`: Stop an ongoing tagging process.
"""

__mod_name__ = "Extras"

SHRUG_HANDLER = DisableAbleCommandHandler(["shrug", "shg"], shrug)
HUG_HANDLER = DisableAbleCommandHandler("hug", hug)
REACT_HANDLER = DisableAbleCommandHandler("react", react)
TOSS_HANDLER = DisableAbleCommandHandler("toss", toss)
SHOUT_HANDLER = DisableAbleCommandHandler("shout", shout, pass_args=True)
PAT_HANDLER = DisableAbleCommandHandler("pat", pat)
SPANK_HANDLER = DisableAbleCommandHandler("spank", spank)
CUDDLE_HANDLER = DisableAbleCommandHandler("cuddle", cuddle)
KISS_HANDLER = DisableAbleCommandHandler("kiss", kiss)
WIKI_HANDLER = DisableAbleCommandHandler("wiki", wiki)
JUDGE_HANDLER = DisableAbleCommandHandler("judge", judge)
WEEBIFY_HANDLER = DisableAbleCommandHandler("weebify", weebify, pass_args=True)
NSFW_HANDLER = DisableAbleCommandHandler("nsfw", night_api_nsfw, pass_args=True)

# Tag All Handlers
TRACKER_HANDLER = MessageHandler(Filters.all & Filters.group, passive_tracker)
TAGALL_CMD_HANDLER = DisableAbleCommandHandler(["all", "tagall"], tag_all, pass_args=True)
CANCEL_CMD_HANDLER = DisableAbleCommandHandler(["cancelall", "untagall", "stopall"], cancel_tag_all)
TAGALL_REGEX_HANDLER = MessageHandler(Filters.regex(r"(?i)^@all(.*)"), tag_all_regex)

dispatcher.add_handler(SHRUG_HANDLER)
dispatcher.add_handler(HUG_HANDLER)
dispatcher.add_handler(REACT_HANDLER)
dispatcher.add_handler(SHOUT_HANDLER)
dispatcher.add_handler(TOSS_HANDLER)
dispatcher.add_handler(PAT_HANDLER)
dispatcher.add_handler(SPANK_HANDLER)
dispatcher.add_handler(CUDDLE_HANDLER)
dispatcher.add_handler(KISS_HANDLER)
dispatcher.add_handler(WIKI_HANDLER)
dispatcher.add_handler(JUDGE_HANDLER)
dispatcher.add_handler(WEEBIFY_HANDLER)
dispatcher.add_handler(NSFW_HANDLER)

# Add tag handlers (passive tracker gets group 10 so it doesn't block other message handlers)
dispatcher.add_handler(TRACKER_HANDLER, group=10)
dispatcher.add_handler(TAGALL_CMD_HANDLER)
dispatcher.add_handler(CANCEL_CMD_HANDLER)
dispatcher.add_handler(TAGALL_REGEX_HANDLER)
