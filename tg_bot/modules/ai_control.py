"""Own AI admin: the group owner orders the bot in plain language.

The owner sends `/ai <instruction>` (e.g. `/ai mute @bob for 1h`). A free
open-source model on Groq (OpenAI-compatible function calling) turns the
instruction into a whitelisted admin action. Destructive actions require the
owner to confirm with `/ai yes <token>` before anything is executed.

Only the chat's creator may use this. Any member who tries gets a warning.
"""
import json
import os
import random
import time

import requests

from telegram import Bot, Update, ParseMode
from telegram.ext import Filters
from telegram.ext.dispatcher import run_async
from telegram.utils.helpers import escape_markdown
from typing import List

from tg_bot import dispatcher, TOKEN
from tg_bot.modules.disable import DisableAbleCommandHandler
from tg_bot.modules.admin import _full_promote_payload

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
AI_MODEL = os.environ.get("AI_CONTROL_MODEL", "llama-3.3-70b-versatile")

# Some models (llama-3.3-70b-versatile) are gated by account tier and return
# 404 for free keys. If the configured model fails, walk this list of broadly
# available models until one answers.
GROQ_FALLBACK_MODELS = [
    "llama-3.1-8b-instant",
    "qwen/qwen3-32b",
    "openai/gpt-oss-20b",
    "moonshotai/kimi-k2-instruct",
]

_PENDING_TTL = 300          # confirmation token lifetime (seconds)
_CONFIRM_COOLDOWN = 3       # seconds between owner commands

_pending = {}               # (chat_id, user_id) -> {"token", "tool_call", "expires"}
_last_command = {}          # (chat_id, user_id) -> timestamp

_OWNER_ONLY_MSG = (
    "⚠️ Only admins can use this.\n"
    "Please promote me to admin and have an admin run /ai."
)

# Actions that need a second "yes" from the owner before executing.
_DESTRUCTIVE = {"promote", "demote", "ban", "unban", "kick", "mute", "unmute"}

AI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "promote",
            "description": "Promote a chat member to administrator.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "@username or numeric user id of the member to promote"},
                    "level": {"type": "string", "enum": ["full", "basic"], "description": "full mirrors the bot's rights; basic gives day-to-day mod rights"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "demote",
            "description": "Remove admin rights from a chat member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "@username or numeric user id of the admin to demote"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ban",
            "description": "Ban a member from the group.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "@username or numeric user id of the member to ban"},
                    "reason": {"type": "string", "description": "optional reason"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unban",
            "description": "Unban a previously banned member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "@username or numeric user id of the member to unban"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kick",
            "description": "Kick a member out of the group (they can rejoin).",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "@username or numeric user id of the member to kick"},
                    "reason": {"type": "string", "description": "optional reason"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mute",
            "description": "Mute a member (stop them sending messages).",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "@username or numeric user id of the member to mute"},
                    "duration": {"type": "string", "description": "duration like '1h', '30m', '2d'; omit for permanent"},
                    "reason": {"type": "string", "description": "optional reason"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unmute",
            "description": "Restore messaging rights for a muted member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "@username or numeric user id of the member to unmute"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pin",
            "description": "Pin a message in the group. The admin must reply to the message they want pinned.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unpin",
            "description": "Unpin the current pinned message in the group.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chat_status",
            "description": "Report current bot rights and member counts for this chat. Safe, instant.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_SYSTEM_PROMPT = (
    "You are PhoenixBot's admin assistant. You act only for the group creator or "
    "an administrator, and only by calling the provided tools. Never call a tool "
    "unless an admin explicitly asked for that admin action. A 'target' must be "
    "exactly the @username or numeric user id given; if it is unclear, ask instead "
    "of guessing. If no tool applies, answer in short, friendly text. Never invent "
    "actions."
)


def _is_admin(chat, user_id):
    try:
        return chat.get_member(user_id).status in ("creator", "administrator")
    except Exception:
        return False


def _resolve_member(chat, target, mention_map=None):
    target = (target or "").strip()
    if not target:
        return None
    if target.lower() == "me":
        return None  # resolved by caller with the owner id
    if target.startswith("@"):
        target = target[1:]
    if not target:
        return None
    if target.isdigit():
        return int(target)
    if mention_map:
        user_id = mention_map.get(target.lower())
        if user_id is not None:
            return user_id
    try:
        for admin in chat.get_administrators():
            user = admin.user
            if user.username and user.username.lower() == target.lower():
                return user.id
            if user.first_name and user.first_name.lower() == target.lower():
                return user.id
    except Exception:
        pass
    return None


def _parse_duration(text):
    text = (text or "").strip().lower()
    if not text:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        amount = int(text.rstrip("smhdw"))
        unit = text[-1] if text[-1] in units else "m"
        if amount <= 0:
            return None
        return amount * units[unit]
    except Exception:
        return None


def _openai_send(model, messages, tool_choice):
    return requests.post(
        GROQ_URL,
        headers={"Authorization": "Bearer {}".format(GROQ_API_KEY), "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "tools": AI_TOOLS, "tool_choice": tool_choice},
        timeout=40,
    )


def _call_groq(messages, tool_choice="auto"):
    models = [AI_MODEL] + [m for m in GROQ_FALLBACK_MODELS if m != AI_MODEL]
    last_error = ""
    for model in models:
        response = _openai_send(model, messages, tool_choice)
        if response.status_code == 200:
            choices = response.json().get("choices") or []
            if not choices:
                raise RuntimeError("Empty Groq response")
            return choices[0].get("message", {})
        last_error = "Groq {} for model {}: {}".format(
            response.status_code, model, response.text[:200])
    raise RuntimeError(last_error)


def _get_admin_actions_text(chat, bot_id):
    try:
        member = chat.get_member(bot_id)
        rights = [
            f for f in ("can_change_info", "can_delete_messages", "can_invite_users",
                        "can_restrict_members", "can_pin_messages", "can_promote_members")
            if getattr(member, f, False)
        ]
        title = "full admin" if len(rights) == 6 else (", ".join(rights) or "no admin rights")
        count = 0
        try:
            count = chat.get_members_count()
        except Exception:
            pass
        return "Bot: {} | members: {}".format(title, count)
    except Exception as e:
        return "error: {}".format(e)


def _execute_tool(bot, chat, user, tool_call, message=None):
    """Run one whitelisted tool call. Returns (ok, reply_text)."""
    name = tool_call.get("name")
    try:
        args = json.loads(tool_call.get("arguments") or "{}")
    except Exception:
        args = {}

    if name == "chat_status":
        return True, _get_admin_actions_text(chat, bot.id)

    if name == "pin":
        if message is None or not message.reply_to_message:
            return False, "Reply to the message you want me to pin, then run /ai pin."
        try:
            bot.pin_chat_message(chat.id, message.reply_to_message.message_id)
            return True, "Pinned. 📌"
        except Exception as e:
            return False, "Pin failed: {}".format(e)

    if name == "unpin":
        try:
            bot.unpin_chat_message(chat.id)
            return True, "Unpinned. ✅"
        except Exception as e:
            return False, "Unpin failed: {}".format(e)

    target = args.get("target", "").strip()
    if args.get("target_id") is not None:
        target = int(args["target_id"])
    elif target.lower() == "me":
        target = user.id
    else:
        target_id = _resolve_member(chat, target)
        if not target_id:
            return False, "I couldn't find a member for target '{}'. Give an exact @username or numeric id.".format(target)
        target = target_id

    if target == bot.id:
        return False, "I won't do that to myself."

    try:
        if name == "promote":
            if args.get("level") == "full":
                payload = _full_promote_payload(bot.id, chat.id, target)
                if payload is None:
                    return False, "Couldn't read the bot's own rights; can't promote."
                res = requests.post("https://api.telegram.org/bot{}/promoteChatMember".format(TOKEN),
                                    json=payload, timeout=15)
                if res.status_code != 200 or not res.json().get("ok"):
                    desc = "unknown"
                    try:
                        desc = res.json().get("description", "unknown")
                    except Exception:
                        pass
                    return False, "Full promote failed: {}".format(desc)
            else:
                member = chat.get_member(bot.id)
                bot.promote_chat_member(
                    chat.id, target,
                    can_change_info=False,
                    can_post_messages=False,
                    can_edit_messages=False,
                    can_delete_messages=bool(getattr(member, "can_delete_messages", False)),
                    can_invite_users=bool(getattr(member, "can_invite_users", False)),
                    can_restrict_members=bool(getattr(member, "can_restrict_members", False)),
                    can_pin_messages=bool(getattr(member, "can_pin_messages", False)),
                    can_promote_members=False,
                )
            return True, "Promoted {} ({}).".format(target, args.get("level", "basic"))

        elif name == "demote":
            bot.promote_chat_member(
                chat.id, target,
                can_change_info=False,
                can_post_messages=False,
                can_edit_messages=False,
                can_delete_messages=False,
                can_invite_users=False,
                can_restrict_members=False,
                can_pin_messages=False,
                can_promote_members=False,
            )
            return True, "Demoted {}.".format(target)

        elif name == "ban":
            bot.kick_chat_member(chat.id, target)
            return True, "Banned {}.".format(target)

        elif name == "unban":
            bot.unban_chat_member(chat.id, target)
            return True, "Unbanned {}.".format(target)

        elif name == "kick":
            bot.kick_chat_member(chat.id, target)
            bot.unban_chat_member(chat.id, target)
            return True, "Kicked {}.".format(target)

        elif name == "mute":
            seconds = _parse_duration(args.get("duration"))
            until = int(time.time()) + seconds if seconds else None
            bot.restrict_chat_member(
                chat.id, target,
                can_send_messages=False,
                can_send_media_messages=False,
                until_date=until,
            )
            label = "permanently" if until is None else "for {}".format(args.get("duration"))
            return True, "Muted {} {}.".format(target, label)

        elif name == "unmute":
            bot.restrict_chat_member(
                chat.id, target,
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
            return True, "Unmuted {}.".format(target)

    except Exception as e:
        return False, "Failed: {}".format(e)

    return False, "Unknown action '{}'.".format(name)


def _summarize(bot, chat, user, tool_name, ok, result):
    try:
        msg = _call_groq(
            [
                {"role": "system", "content": "You are PhoenixBot's admin assistant. Reply in one short line."},
                {"role": "user", "content": "The owner asked to {} via the {} tool. Outcome: {} — {}".format(
                    result, tool_name, "success" if ok else "failure", result)},
            ],
            tool_choice="none",
        )
        return msg.get("content") or result
    except Exception:
        return result


@run_async
def ai_admin(bot: Bot, update: Update, args: List[str]):
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user
    key = (chat.id, user.id)

    if not GROQ_API_KEY:
        msg.reply_text("❌ `GROQ_API_KEY` is not configured.", parse_mode=ParseMode.MARKDOWN)
        return

    if chat.type == "private":
        msg.reply_text("This works only inside a group.")
        return

    if not _is_admin(chat, user.id):
        msg.reply_text(_OWNER_ONLY_MSG)
        return

    # Handle confirmation: /ai yes <token> | /ai no <token> (before the
    # command cooldown so an immediate confirmation is never dropped)
    if args:
        first = args[0].lower()
        if first in ("yes", "confirm") and len(args) >= 2:
            _confirm(bot, chat, user, args[1], execute=True)
            return
        if first in ("no", "deny", "cancel") and len(args) >= 2:
            _confirm(bot, chat, user, args[1], execute=False)
            return

    now = time.time()
    if now - _last_command.get(key, 0) < _CONFIRM_COOLDOWN:
        return
    _last_command[key] = now

    instruction = " ".join(args)
    if not instruction:
        msg.reply_text(
            "Tell me what to do, e.g. `/ai mute @bob for 1h`, `/ai promote @sam full`, `/ai ban @spammer`, "
            "`/ai pin` (reply to a message first).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    bot.send_chat_action(chat_id=chat.id, action="typing")
    status = _get_admin_actions_text(chat, bot.id)
    try:
        reply = _call_groq([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "Chat: {} ({}). {}\nOwner instruction: {}".format(
                chat.title or "?", chat.type, status, instruction)},
        ])
    except Exception as e:
        msg.reply_text("❌ AI request failed: {}".format(e))
        return

    tool_calls = reply.get("tool_calls")
    if not tool_calls:
        msg.reply_text(reply.get("content") or "I didn't catch that — rephrase?")
        return

    tool_call = tool_calls[0].get("function", {})

    # Pre-resolve @mentions from the message. Telegram attaches the full User
    # (id + username) to mention entities, so we can resolve non-admin members
    # whose username isn't in the admin list.
    mention_map = {}
    for entity in (msg.entities or []):
        if entity.type == "mention" and entity.user and entity.user.username:
            mention_map[entity.user.username.lower()] = entity.user.id
    if mention_map:
        try:
            tool_args = json.loads(tool_call.get("arguments") or "{}")
        except Exception:
            tool_args = {}
        target = tool_args.get("target", "").strip()
        if target and target.lower() != "me":
            resolved = _resolve_member(chat, target, mention_map)
            if resolved:
                tool_args["target_id"] = resolved
                tool_call = dict(tool_call)
                tool_call["arguments"] = json.dumps(tool_args)

    if tool_call.get("name") not in _DESTRUCTIVE:
        ok, result = _execute_tool(bot, chat, user, tool_call, message=msg)
        msg.reply_text(result)
        return

    token = "%04x" % random.randrange(16**4)
    _pending[key] = {"token": token, "tool_call": tool_call, "expires": time.time() + _PENDING_TTL}
    msg.reply_text(
        "⚠️ Ready to **{}**: `{}`\n\n"
        "Reply `/ai yes {}` to confirm, or `/ai no {}` to cancel. (expires in 5 min)".format(
            tool_call.get("name"),
            tool_call.get("arguments") or "{}",
            token,
            token,
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


def _confirm(bot, chat, user, token, execute):
    key = (chat.id, user.id)
    pending = _pending.get(key)
    if not pending:
        return
    if pending["token"] != token or pending.get("expires", 0) < time.time():
        _pending.pop(key, None)
        return

    if not _is_admin(chat, user.id):
        return

    _pending.pop(key, None)
    tool_call = pending["tool_call"]
    if not execute:
        bot.send_message(chat.id, "Cancelled. ✅")
        return

    ok, result = _execute_tool(bot, chat, user, tool_call)
    summary = _summarize(bot, chat, user, tool_call.get("name"), ok, result)
    bot.send_message(
        chat.id,
        escape_markdown(summary),
        parse_mode=ParseMode.MARKDOWN,
    )


AI_HANDLER = DisableAbleCommandHandler("ai", ai_admin, pass_args=True, filters=Filters.group)
dispatcher.add_handler(AI_HANDLER)