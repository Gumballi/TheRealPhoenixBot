import html
import json
from typing import Optional, List

import requests
from telegram import Message, Chat, Update, Bot, User
from telegram import ParseMode
from telegram.error import BadRequest
from telegram.ext import CommandHandler, Filters
from telegram.ext.dispatcher import run_async
from telegram.utils.helpers import escape_markdown, mention_html

from tg_bot import dispatcher, SUDO_USERS, TOKEN
from tg_bot.modules.disable import DisableAbleCommandHandler
from tg_bot.modules.helper_funcs.chat_status import bot_admin, can_promote, user_admin, can_pin
from tg_bot.modules.helper_funcs.extraction import extract_user, extract_user_and_text
from tg_bot.modules.log_channel import loggable
from tg_bot.modules.sql import ownerlock_sql as olock
from tg_bot.modules.sql import users_sql as usql

ADMIN_PERMISSION_FIELDS = (
    "can_manage_chat", "can_post_messages", "can_edit_messages",
    "can_change_info", "can_delete_messages", "can_restrict_members",
    "can_invite_users", "can_pin_messages", "can_promote_members",
    "can_manage_video_chats", "can_manage_topics", "can_post_stories",
    "can_edit_stories", "can_delete_stories", "can_manage_tags",
    "can_send_welcome_messages",
)


def _get_bot_permissions(bot_member, chat_id):
    permissions = {
        field: bool(getattr(bot_member, field, False))
        for field in ADMIN_PERMISSION_FIELDS
    }
    try:
        response = requests.get(
            "https://api.telegram.org/bot{}/getChatMember".format(TOKEN),
            params={"chat_id": chat_id, "user_id": bot_member.user.id},
            timeout=10,
        )
        data = response.json()
        raw_member = data.get("result", {}) if isinstance(data, dict) else {}
        raw_permissions = raw_member.get("permissions", {})
        if isinstance(raw_permissions, dict):
            for field in ADMIN_PERMISSION_FIELDS:
                if field in raw_permissions:
                    permissions[field] = bool(raw_permissions[field])
    except Exception:
        pass
    return permissions


@run_async
@bot_admin
@can_promote
@user_admin
@loggable
def promote(bot: Bot, update: Update, args: List[str]) -> str:
    chat_id = update.effective_chat.id
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    promoter = chat.get_member(user.id)
    if not (promoter.can_promote_members or promoter.status == "creator") and user.id not in SUDO_USERS:
        message.reply_text("You don't have the necessary rights to do that!")
        return ""
    promote_type = "basic"
    clean_args = []
    for arg in args:
        if arg.lower() == "full":
            promote_type = "full"
        elif arg.lower() == "basic":
            promote_type = "basic"
        else:
            clean_args.append(arg)
    user_id = extract_user(message, clean_args)
    if not user_id:
        message.reply_text("You don't seem to be referring to a user.")
        return ""
    if not olock.can_act(bot, update, user_id, ["promote"]):
        return ""
    user_member = chat.get_member(user_id)
    if user_member.status in ('administrator', 'creator'):
        message.reply_text("How am I meant to promote someone that's already an admin?")
        return ""
    if user_id == bot.id:
        message.reply_text("I can't promote myself! Get an admin to do it for me.")
        return ""
    bot_member = chat.get_member(bot.id)
    if promote_type == "full":
        url = "https://api.telegram.org/bot{}/promoteChatMember".format(TOKEN)
        payload = {"chat_id": chat_id, "user_id": user_id, "is_anonymous": False}
        payload.update(_get_bot_permissions(bot_member, chat_id))
        res = requests.post(url, json=payload)
        if res.status_code != 200 or not res.json().get("ok"):
            try:
                err_desc = res.json().get("description", "Unknown error")
            except Exception:
                err_desc = res.text
            message.reply_text("Failed to fully promote user: {}".format(err_desc))
            return ""
    else:
        bot.promoteChatMember(chat_id, user_id, can_change_info=False,
                              can_post_messages=False, can_edit_messages=False,
                              can_delete_messages=bool(getattr(bot_member, "can_delete_messages", False)),
                              can_invite_users=bool(getattr(bot_member, "can_invite_users", False)),
                              can_restrict_members=bool(getattr(bot_member, "can_restrict_members", False)),
                              can_pin_messages=bool(getattr(bot_member, "can_pin_messages", False)),
                              can_promote_members=False)
    if olock.is_owner(update):
        olock.owner_action(chat_id, user_id, "promote", "demote")
    message.reply_text("Successfully promoted ({})!".format(promote_type.capitalize()))
    return "<b>{}:</b>\n#PROMOTED ({})\n<b>Admin:</b> {}\n<b>User:</b> {}".format(html.escape(chat.title), promote_type.capitalize(), mention_html(user.id, user.first_name), mention_html(user_member.user.id, user_member.user.first_name))


@run_async
@bot_admin
@can_promote
@user_admin
def set_title(bot: Bot, update: Update, args):
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    promoter = chat.get_member(user.id)
    if not (promoter.can_promote_members or promoter.status == "creator") and user.id not in SUDO_USERS:
        message.reply_text("You don't have the necessary rights to do that!")
        return
    user_id, title = extract_user_and_text(message, args)
    if not user_id:
        message.reply_text("You don't seem to be referring to a user.")
        return
    if not title:
        message.reply_text("There's no title...")
        return
    response = requests.post("https://api.telegram.org/bot{}/setChatAdministratorCustomTitle".format(TOKEN), params={"chat_id": chat.id, "user_id": user_id, "custom_title": title})
    text = "An error occurred:\n`{}`".format(json.loads(response.text).get('description')) if response.status_code != 200 else "Successfully set title to `{}`!".format(title)
    message.reply_text(text, parse_mode="MARKDOWN")


@run_async
@bot_admin
@can_promote
@user_admin
@loggable
def demote(bot: Bot, update: Update, args: List[str]) -> str:
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user
    promoter = chat.get_member(user.id)
    if not (promoter.can_promote_members or promoter.status == "creator") and user.id not in SUDO_USERS:
        message.reply_text("You don't have the necessary rights to do that!")
        return ""
    user_id = extract_user(message, args)
    if not user_id:
        message.reply_text("You don't seem to be referring to a user.")
        return ""
    if not olock.can_act(bot, update, user_id, ["demote"]):
        return ""
    user_member = chat.get_member(user_id)
    if user_member.status == 'creator':
        message.reply_text("This person CREATED the chat, how would I demote them?")
        return ""
    if user_member.status != 'administrator':
        message.reply_text("Can't demote what wasn't promoted!")
        return ""
    if user_id == bot.id:
        message.reply_text("I can't demote myself! Get an admin to do it for me.")
        return ""
    try:
        bot.promoteChatMember(int(chat.id), int(user_id), can_change_info=False, can_post_messages=False, can_edit_messages=False, can_delete_messages=False, can_invite_users=False, can_restrict_members=False, can_pin_messages=False, can_promote_members=False)
        if olock.is_owner(update):
            olock.owner_action(chat.id, user_id, "demote", "promote")
        message.reply_text("Successfully demoted!")
        return ""
    except BadRequest:
        message.reply_text("Could not demote. I might not be admin, or the admin status was appointed by another user, so I can't act upon them!")
        return ""


@run_async
def adminlist(bot: Bot, update: Update):
    administrators = update.effective_chat.get_administrators()
    text = "Admins in <b>{}</b>:".format(update.effective_chat.title or "this chat")
    for admin in administrators:
        user = admin.user
        text += "\n • <a href=\"tg://user?id={}\">{}</a>".format(user.id, html.escape(user.first_name))
    update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


def __chat_settings__(chat_id, user_id):
    return "You are *admin*: `{}`".format(dispatcher.bot.get_chat_member(chat_id, user_id).status in ("administrator", "creator"))

__help__ = """
 - /adminlist: list of admins in a chat
 - /promote full: promotes with all permissions held by the bot
"""
__mod_name__ = "Admin"
PIN_HANDLER = CommandHandler("pin", lambda bot, update, *args: None, pass_args=True, filters=Filters.group)
UNPIN_HANDLER = CommandHandler("unpin", lambda bot, update: None, filters=Filters.group)
INVITE_HANDLER = CommandHandler("link", lambda bot, update: None, filters=Filters.group)
PROMOTE_HANDLER = DisableAbleCommandHandler("promote", promote, pass_args=True, filters=Filters.group)
SET_TITLE_HANDLER = CommandHandler("settitle", set_title, pass_args=True, filters=Filters.group)
DEMOTE_HANDLER = DisableAbleCommandHandler("demote", demote, pass_args=True, filters=Filters.group)
ADMINLIST_HANDLER = DisableAbleCommandHandler("adminlist", adminlist, filters=Filters.group)
dispatcher.add_handler(PIN_HANDLER)
dispatcher.add_handler(UNPIN_HANDLER)
dispatcher.add_handler(INVITE_HANDLER)
dispatcher.add_handler(PROMOTE_HANDLER)
dispatcher.add_handler(DEMOTE_HANDLER)
dispatcher.add_handler(ADMINLIST_HANDLER)
dispatcher.add_handler(SET_TITLE_HANDLER)
