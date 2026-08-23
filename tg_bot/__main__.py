import os
import collections
import collections.abc
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer

import sys
if __name__ == "__main__":
    sys.modules["tg_bot.__main__"] = sys.modules["__main__"]
    
# Patch older libraries expecting Mapping in the top-level collections module
collections.Mapping = collections.abc.Mapping
collections.MutableMapping = collections.abc.MutableMapping
collections.Sequence = collections.abc.Sequence
collections.MutableSequence = collections.abc.MutableSequence

# Intercept and fix malclient-upgraded constructor compatibility
# (myanimelist.py now talks to the MAL API directly via requests, so the
# monkeypatch is no longer needed and was removed.)

import datetime
import importlib
import re
from typing import Optional, List

from telegram import Message, Chat, Update, Bot, User
from telegram import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import Unauthorized, BadRequest, TimedOut, NetworkError, ChatMigrated, TelegramError
from telegram.ext import CommandHandler, Filters, MessageHandler, CallbackQueryHandler
from telegram.ext.dispatcher import run_async, DispatcherHandlerStop, Dispatcher
# Added escape_markdown to avoid NameError crashes down the line
from telegram.utils.helpers import escape_markdown 

# We patch the Dispatcher class *before* importing tg_bot and initializing the dispatcher instance!
CHATS_CNT = {}
CHATS_TIME = {}

def process_update(self, update):
    # ── DEBUG: log every update entering the dispatcher ──
    try:
        ec = update.effective_chat
        em = update.effective_message
        LOGGER.info(
            "[DBG_UPDATE] chat_id=%s chat_type=%s msg_type=%s has_text=%s",
            ec.id if ec else "NONE",
            ec.type if ec else "NONE",
            type(em).__name__ if em else "NONE",
            bool(em.text) if em else False,
        )
    except Exception:
        pass
    # ── END DEBUG ──

    if isinstance(update, TelegramError):
        try:
            self.dispatch_error(None, update)
        except Exception:
            self.logger.exception('An uncaught error was raised while handling the error')
        return

    if not update.effective_chat:
        return

    now = datetime.datetime.utcnow()
    cnt = CHATS_CNT.get(update.effective_chat.id, 0)

    if len(CHATS_TIME) > 1000:
        cutoff = now - datetime.timedelta(0, 3600)
        for chat_id in [cid for cid, t in CHATS_TIME.items() if t < cutoff]:
            CHATS_TIME.pop(chat_id, None)
            CHATS_CNT.pop(chat_id, None)

    t = CHATS_TIME.get(update.effective_chat.id, datetime.datetime(1970, 1, 1))
    if t and now > t + datetime.timedelta(0, 1):
        CHATS_TIME[update.effective_chat.id] = now
        cnt = 0
    else:
        cnt += 1

    if cnt > 10:
        return

    CHATS_CNT[update.effective_chat.id] = cnt
    
    # Process updates inside a comprehensive try/finally sweep to handle dead database states
    try:
        for group in self.groups:
            try:
                for handler in (x for x in self.handlers[group] if x.check_update(update)):
                    handler.handle_update(update, self)
                    break
            except DispatcherHandlerStop:
                self.logger.debug('Stopping further handlers due to DispatcherHandlerStop')
                break
            except TelegramError as te:
                self.logger.warning('A TelegramError was raised while processing the Update')
                try:
                    self.dispatch_error(update, te)
                except DispatcherHandlerStop:
                    self.logger.debug('Error handler stopped further handlers')
                    break
                except Exception:
                    self.logger.exception('An uncaught error was raised while handling the error')
            except Exception:
                self.logger.exception('An uncaught error was raised while processing the update')
    finally:
        # Forces a database rollback if any module left a broken or uncommitted transaction state
        try:
            SESSION.rollback()
        except Exception:
            pass

# Apply the patch immediately
Dispatcher.process_update = process_update

# Now we can safely import dispatcher and updater
from tg_bot import dispatcher, updater, TOKEN, WEBHOOK, OWNER_ID, DONATION_LINK, CERT_PATH, PORT, URL, LOGGER, \
    ALLOW_EXCL
# Import database session to safeguard against transaction lockups
from tg_bot.modules.sql import SESSION
# needed to dynamically load modules
from tg_bot.modules import ALL_MODULES
from tg_bot.modules.helper_funcs.chat_status import is_user_admin
from tg_bot.modules.helper_funcs.misc import paginate_modules

PM_START_TEXT = """
Hi {}, my name is {}!

You can find the list of available commands with /help.
"""

BOT_IMAGE = "https://telegra.ph/file/93612a540608640355f20.mp4"

HELP_STRINGS = """
Hey there! My name is *{}*.
I'm a modular group management bot with a few fun extras! Have a look at the following for an idea of some of \
the things I can help you with.
If you have any questions on how to use me, head over to @PhoenixSupport

*Main* commands available:
 - /start: start the bot
 - /help: PM's you this message.
 - /help <module name>: PM's you info about that module.
 - /donate: information about how to donate!
 - /settings:
   - in PM: will send you your settings for all supported modules.
   - in a group: will redirect you to pm, with all that chat's settings.

{}
And the following:
""".format(dispatcher.bot.first_name, "" if not ALLOW_EXCL else "\nAll commands can either be used with / or !.\n")

DONATE_STRING = "I don't currently have a donation link set up, but thanks for wanting to support {}!".format(dispatcher.bot.first_name)

IMPORTED = {}
MIGRATEABLE = []
HELPABLE = {}
STATS = []
USER_INFO = []
DATA_IMPORT = []
DATA_EXPORT = []

CHAT_SETTINGS = {}
USER_SETTINGS = {}

GDPR = []

for module_name in ALL_MODULES:
    try:
        imported_module = importlib.import_module("tg_bot.modules." + module_name)
    except Exception as e:
        LOGGER.error(f"Failed to import module {module_name}: {e}")
        continue
    if not hasattr(imported_module, "__mod_name__"):
        imported_module.__mod_name__ = imported_module.__name__

    if imported_module.__mod_name__.lower() not in IMPORTED:
        IMPORTED[imported_module.__mod_name__.lower()] = imported_module
    else:
        raise Exception("Can't have two modules with the same name! Please change one")

    if hasattr(imported_module, "__help__") and imported_module.__help__:
        HELPABLE[imported_module.__mod_name__.lower()] = imported_module

    # Chats to migrate on chat_migrated events
    if hasattr(imported_module, "__migrate__"):
        MIGRATEABLE.append(imported_module)

    if hasattr(imported_module, "__stats__"):
        STATS.append(imported_module)

    if hasattr(imported_module, "__gdpr__"):
        GDPR.append(imported_module)

    if hasattr(imported_module, "__user_info__"):
        USER_INFO.append(imported_module)

    if hasattr(imported_module, "__import_data__"):
        DATA_IMPORT.append(imported_module)

    if hasattr(imported_module, "__export_data__"):
        DATA_EXPORT.append(imported_module)

    if hasattr(imported_module, "__chat_settings__"):
        CHAT_SETTINGS[imported_module.__mod_name__.lower()] = imported_module

    if hasattr(imported_module, "__user_settings__"):
        USER_SETTINGS[imported_module.__mod_name__.lower()] = imported_module


# do not async
def send_help(chat_id, text, keyboard=None):
    if not keyboard:
        keyboard = InlineKeyboardMarkup(paginate_modules(0, HELPABLE, "help"))
    dispatcher.bot.send_message(chat_id=chat_id,
                                text=text,
                                parse_mode=ParseMode.MARKDOWN,
                                reply_markup=keyboard)


@run_async
def test(bot: Bot, update: Update):
    update.effective_message.reply_text("This person edited a message")
    print(update.effective_message)


@run_async
def start(bot: Bot, update: Update, args: List[str]):
    if update.effective_chat.type == "private":
        if args:
            if args[0].lower() == "help":
                send_help(update.effective_chat.id, HELP_STRINGS)

            elif args[0].lower().startswith("stngs_"):
                match = re.match("stngs_(.*)", args[0].lower())
                chat = dispatcher.bot.getChat(match.group(1))

                if is_user_admin(chat, update.effective_user.id):
                    send_settings(match.group(1), update.effective_user.id, False)
                else:
                    send_settings(match.group(1), update.effective_user.id, True)

            elif args[0][1:].isdigit() and "rules" in IMPORTED:
                IMPORTED["rules"].send_rules(update, args[0], from_pm=True)

        else:
            first_name = update.effective_user.first_name
            update.effective_message.reply_animation(
                BOT_IMAGE,
                caption=PM_START_TEXT.format(escape_markdown(first_name), escape_markdown(bot.first_name), OWNER_ID),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text="Add Pɧơɛnıх to your group.",
                                url="t.me/{}?startgroup=botstart".format(bot.username),
                            )
                        ]
                    ]
                ),
            )
    else:
        update.effective_message.reply_text("Yo, why'd you summon me?")


# for test purposes
def error_callback(bot, update, error):
    try:
        raise error
    except Unauthorized:
        print("no nono1")
        print(error)
    except BadRequest:
        print("no nono2")
        print("BadRequest caught")
        print(error)
    except TimedOut:
        print("no nono3")
    except NetworkError:
        print("no nono4")
    except ChatMigrated as err:
        print("no nono5")
        print(err)
    except TelegramError:
        print(error)


@run_async
def help_button(bot: Bot, update: Update):
    query = update.callback_query
    mod_match = re.match(r"help_module\((.+?)\)", query.data)
    prev_match = re.match(r"help_prev\((.+?)\)", query.data)
    next_match = re.match(r"help_next\((.+?)\)", query.data)
    back_match = re.match(r"help_back", query.data)
    try:
        if mod_match:
            module = mod_match.group(1)
            text = "Here is the help for the *{}* module:\n".format(HELPABLE[module].__mod_name__) \
                   + HELPABLE[module].__help__
            query.message.edit_text(text=text,
                                    parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(
                                         [[InlineKeyboardButton(text="Back", callback_data="help_back")]]))

        elif prev_match:
            curr_page = int(prev_match.group(1))
            query.message.edit_text(HELP_STRINGS,
                                     parse_mode=ParseMode.MARKDOWN,
                                     reply_markup=InlineKeyboardMarkup(
                                         paginate_modules(curr_page - 1, HELPABLE, "help")))

        elif next_match:
            next_page = int(next_match.group(1))
            query.message.edit_text(HELP_STRINGS,
                                     parse_mode=ParseMode.MARKDOWN,
                                     reply_markup=InlineKeyboardMarkup(
                                         paginate_modules(next_page + 1, HELPABLE, "help")))

        elif back_match:
            query.message.edit_text(text=HELP_STRINGS,
                                     parse_mode=ParseMode.MARKDOWN,
                                     reply_markup=InlineKeyboardMarkup(paginate_modules(0, HELPABLE, "help")))

        bot.answer_callback_query(query.id)
    except BadRequest as excp:
        if excp.message not in [
            'Message is not modified',
            'Query_id_invalid',
            "Message can't be deleted",
        ]:
            LOGGER.exception('Exception in help buttons. %s', str(query.data))


@run_async
def get_help(bot: Bot, update: Update):
    chat = update.effective_chat
    args = update.effective_message.text.split(None, 1)

    if chat.type != chat.PRIVATE:
        update.effective_message.reply_text("Contact me in PM to get the list of possible commands.",
                                            reply_markup=InlineKeyboardMarkup(
                                                [[InlineKeyboardButton(text="Help",
                                                                       url="t.me/{}?start=help".format(
                                                                           bot.username))]]))
        return

    elif len(args) >= 2 and any(args[1].lower() == x for x in HELPABLE):
        module = args[1].lower()
        text = "Here is the available help for the *{}* module:\n".format(HELPABLE[module].__mod_name__) \
               + HELPABLE[module].__help__
        send_help(chat.id, text, InlineKeyboardMarkup([[InlineKeyboardButton(text="Back", callback_data="help_back")]]))

    else:
        send_help(chat.id, HELP_STRINGS)


def send_settings(chat_id, user_id, user=False):
    if user:
        if USER_SETTINGS:
            settings = "\n\n".join(
                "*{}*:\n{}".format(mod.__mod_name__, mod.__user_settings__(user_id)) for mod in USER_SETTINGS.values())
            dispatcher.bot.send_message(user_id, "These are your current settings:" + "\n\n" + settings,
                                        parse_mode=ParseMode.MARKDOWN)
        else:
            dispatcher.bot.send_message(user_id, "Seems like there aren't any user specific settings available :'(",
                                        parse_mode=ParseMode.MARKDOWN)

    elif CHAT_SETTINGS:
        chat_name = dispatcher.bot.getChat(chat_id).title
        dispatcher.bot.send_message(user_id,
                                    text="Which module would you like to check {}'s settings for?".format(
                                        chat_name),
                                    reply_markup=InlineKeyboardMarkup(
                                        paginate_modules(0, CHAT_SETTINGS, "stngs", chat=chat_id)))
    else:
        dispatcher.bot.send_message(user_id, "Seems like there aren't any chat settings available :'(\nSend this "
                                             "in a group chat you're admin in to find its current settings!",
                                    parse_mode=ParseMode.MARKDOWN)


@run_async
def settings_button(bot: Bot, update: Update):
    query = update.callback_query
    user = update.effective_user
    mod_match = re.match(r"stngs_module\((.+?),(.+?)\)", query.data)
    prev_match = re.match(r"stngs_prev\((.+?),(.+?)\)", query.data)
    next_match = re.match(r"stngs_next\((.+?),(.+?)\)", query.data)
    back_match = re.match(r"stngs_back\((.+?)\)", query.data)
    try:
        if mod_match:
            chat_id = mod_match.group(1)
            module = mod_match.group(2)
            chat = bot.get_chat(chat_id)
            text = "*{}* has the following settings for the *{}* module:\n\n".format(escape_markdown(chat.title),
                                                                                     CHAT_SETTINGS[
                                                                                         module].__mod_name__) + \
                   CHAT_SETTINGS[module].__chat_settings__(chat_id, user.id)
            query.message.reply_text(text=text,
                                     parse_mode=ParseMode.MARKDOWN,
                                     reply_markup=InlineKeyboardMarkup(
                                         [[InlineKeyboardButton(text="Back",
                                                                callback_data="stngs_back({})".format(chat_id))]]))

        elif prev_match:
            chat_id = prev_match.group(1)
            curr_page = int(prev_match.group(2))
            chat = bot.get_chat(chat_id)
            query.message.reply_text("Hi there! There are quite a few settings for {} - go ahead and pick what "
                                     "you're interested in.".format(chat.title),
                                     reply_markup=InlineKeyboardMarkup(
                                         paginate_modules(curr_page - 1, CHAT_SETTINGS, "stngs",
                                                          chat=chat_id)))

        elif next_match:
            chat_id = next_match.group(1)
            next_page = int(next_match.group(2))
            chat = bot.get_chat(chat_id)
            query.message.reply_text("Hi there! There are quite a few settings for {} - go ahead and pick what "
                                     "you're interested in.".format(chat.title),
                                     reply_markup=InlineKeyboardMarkup(
                                         paginate_modules(next_page + 1, CHAT_SETTINGS, "stngs",
                                                          chat=chat_id)))

        elif back_match:
            chat_id = back_match.group(1)
            chat = bot.get_chat(chat_id)
            query.message.reply_text(text="Hi there! There are quite a few settings for {} - go ahead and pick what "
                                          "you're interested in.".format(escape_markdown(chat.title)),
                                     parse_mode=ParseMode.MARKDOWN,
                                     reply_markup=InlineKeyboardMarkup(paginate_modules(0, CHAT_SETTINGS, "stngs",
                                                                                        chat=chat_id)))

        bot.answer_callback_query(query.id)
        query.message.delete()
    except BadRequest as excp:
        if excp.message not in [
            'Message is not modified',
            'Query_id_invalid',
            "Message can't be deleted",
        ]:
            LOGGER.exception('Exception in settings buttons. %s', str(query.data))


@run_async
def get_settings(bot: Bot, update: Update):
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    args = msg.text.split(None, 1)

    if chat.type == chat.PRIVATE:
        send_settings(chat.id, user.id, True)

    elif is_user_admin(chat, user.id):
        text = "Click here to get this chat's settings, as well as yours."
        msg.reply_text(text,
                       reply_markup=InlineKeyboardMarkup(
                           [[InlineKeyboardButton(text="Settings",
                                                  url="t.me/{}?start=stngs_{}".format(
                                                      bot.username, chat.id))]]))
    else:
        text = "Click here to check your settings."


@run_async
def donate(bot: Bot, update: Update):
    user = update.effective_message.from_user
    chat = update.effective_chat

    # Determine what message to send based on your DONATION_LINK variable
    if DONATION_LINK:
        donate_text = "You can donate to support {} [here]({})".format(bot.first_name, DONATION_LINK)
    else:
        donate_text = DONATE_STRING

    # Send the message
    if chat.type == "private":
        update.effective_message.reply_text(donate_text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    else:
        try:
            bot.send_message(user.id, donate_text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
            update.effective_message.reply_text("I've PM'ed you about supporting the bot!")
        except Unauthorized:
            update.effective_message.reply_text("Contact me in PM first to get donation information.")


def migrate_chats(bot: Bot, update: Update):
    msg = update.effective_message
    if msg.migrate_to_chat_id:
        old_chat = update.effective_chat.id
        new_chat = msg.migrate_to_chat_id
    elif msg.migrate_from_chat_id:
        old_chat = msg.migrate_from_chat_id
        new_chat = update.effective_chat.id
    else:
        return

    LOGGER.info("Migrating from %s, to %s", str(old_chat), str(new_chat))
    for mod in MIGRATEABLE:
        mod.__migrate__(old_chat, new_chat)

    LOGGER.info("Successfully migrated!")
    raise DispatcherHandlerStop


def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
        LOGGER.info(f"Dummy background server listening on port {port}")
        server.serve_forever()
    except OSError as e:
        # 98 corresponds to EADDRINUSE (Address already in use)
        if e.errno == 98:
            LOGGER.warning(f"Port {port} is already in use by a parallel deployment worker. Skipping dummy server thread initialization.")
        else:
            raise e


def main():
    # Start the dummy server on a separate background thread to keep Render Free Tier happy
    threading.Thread(target=run_dummy_server, daemon=True).start()

    test_handler = CommandHandler("test", test)
    start_handler = CommandHandler("start", start, pass_args=True)

    help_handler = CommandHandler("help", get_help)
    help_callback_handler = CallbackQueryHandler(help_button, pattern=r"help_")

    settings_handler = CommandHandler("settings", get_settings)
    settings_callback_handler = CallbackQueryHandler(settings_button, pattern=r"stngs_")

    donate_handler = CommandHandler("donate", donate)
    migrate_handler = MessageHandler(Filters.status_update.migrate, migrate_chats)

    dispatcher.add_handler(start_handler)
    dispatcher.add_handler(help_handler)
    dispatcher.add_handler(settings_handler)
    dispatcher.add_handler(help_callback_handler)
    dispatcher.add_handler(settings_callback_handler)
    dispatcher.add_handler(migrate_handler)
    dispatcher.add_handler(donate_handler)

    if WEBHOOK:
        LOGGER.info("Using webhooks.")
        updater.start_webhook(listen="127.0.0.1",
                              port=PORT,
                              url_path=TOKEN)

        if CERT_PATH:
            updater.bot.set_webhook(url=URL + TOKEN,
                                    certificate=open(CERT_PATH, 'rb'))
        else:
            updater.bot.set_webhook(url=URL + TOKEN)

    else:
        LOGGER.info("Using long polling.")
        updater.start_polling(timeout=15, read_latency=4)
    updater.idle()


if __name__ == '__main__':
    LOGGER.info("Successfully loaded modules: " + str(ALL_MODULES))
    main()
