# Modular AI Chatbot module for TheRealPhoenixBot
# Automatically retries transient errors (503/429/overload) and fails over between
# providers (Gemini, Mistral) so a single provider's outage doesn't take /ask down
# entirely. Upgraded with YouTube Transcript extraction.
#
# FIXED (see PR/patch notes):
#   - mention_chatbot no longer fires on every reply in the chat (was matching
#     Filters.reply, which matches ANY reply to ANY message, not just replies
#     to the bot). Now only spawns work when the message actually concerns us.
#   - is_mentioned uses real mention/text_mention entities instead of a raw
#     substring search on message text (avoids false positives).
#   - Added a lightweight per-user cooldown to stop a single user (or a raid)
#     from burning through your AI provider quota.
#   - GEMINI_MODEL is now overridable via env var.
#   - Mistral call failures are surfaced more clearly instead of always
#     collapsing into the generic "neural misfire" message.
#   - /model command with an inline keyboard to pick the per-user provider.
#   - NOTE: Poke was removed - its API is send-only (fire-and-forget, replies arrive
#     on the user's own phone), so it can't produce chat answers.

import io
import os
import time
import logging
import re
import glob
import requests
from collections import defaultdict, deque

from telegram import Bot, Update, ParseMode, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, MessageHandler, Filters, CallbackQueryHandler, run_async
from tg_bot import dispatcher
from tg_bot.modules.disable import DisableAbleCommandHandler

LOGGER = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are Phoenix, a helpful, authentic, and witty AI companion bot in a Telegram chat. "
    "Respond concisely, keep formatting clean (use basic markdown safely), and match the conversational tone of the user. "
    "Multiple users may talk in the same group chat, so pay attention to the name shown for each message "
    "and address the person who asked. Keep up with the recent conversation history when answering."
)

# ---------------------------------------------------------------------------
# Gemini setup
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("AI_API_KEY") or os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")  # overridable if Google renames/retires it
# If the configured model 404s (renamed/retired, or typo in GEMINI_MODEL), try
# these in order until one actually exists for the API key in use.
GEMINI_FALLBACK_MODELS = [
    "gemini-3-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]
gemini_client = None
genai_types = None

if GEMINI_API_KEY:
    try:
        from google import genai
        from google.genai import types as genai_types
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        LOGGER.error(f"[ai] Failed to initialize Gemini client: {e}")
else:
    LOGGER.warning("[ai] AI_API_KEY / GEMINI_API_KEY not set - Gemini provider disabled.")

# ---------------------------------------------------------------------------
# Mistral setup
# ---------------------------------------------------------------------------
try:
    from tg_bot import MISTRAL_API_KEY
except ImportError:
    MISTRAL_API_KEY = None

if not MISTRAL_API_KEY:
    MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")

MISTRAL_MODEL = "mistral-large-latest"
mistral_client = None
mistral_supports_chat_complete = False

if MISTRAL_API_KEY:
    try:
        # Use a shim to handle both mistralai v1 and v2 SDKs cleanly
        try:
            from mistralai.client import Mistral
        except ImportError:
            from mistralai import Mistral

        mistral_client = Mistral(api_key=MISTRAL_API_KEY)

        # Verify the resolved SDK actually exposes the call shape we use below.
        # Older mistralai releases (pre-v1) don't have client.chat.complete(),
        # which previously caused every Mistral call to fail silently and get
        # swallowed by the retry/failover logic.
        mistral_supports_chat_complete = hasattr(mistral_client, "chat") and hasattr(
            mistral_client.chat, "complete"
        )
        if not mistral_supports_chat_complete:
            LOGGER.error(
                "[ai] Installed mistralai SDK does not support client.chat.complete() - "
                "Mistral fallback will be disabled. Pin `mistralai>=1.0.0` in requirements.txt."
            )
    except Exception as e:
        LOGGER.error(f"[ai] Failed to initialize Mistral client: {e}")
else:
    LOGGER.warning("[ai] MISTRAL_API_KEY not set - Mistral fallback disabled.")

# Order providers are tried in. Override via env if you want Mistral tried first
PROVIDER_ORDER = [
    p.strip().lower()
    for p in os.environ.get("AI_PROVIDER_ORDER", "gemini,mistral").split(",")
    if p.strip()
]

MAX_RETRIES_PER_PROVIDER = 2
BACKOFF_BASE_SECONDS = 1.5
TRANSIENT_MARKERS = ("503", "UNAVAILABLE", "429", "rate limit", "overloaded", "high demand", "timeout")

# ---------------------------------------------------------------------------
# Per-user cooldown (prevents a single user/raid from burning your AI quota)
# ---------------------------------------------------------------------------
COOLDOWN_SECONDS = int(os.environ.get("AI_COOLDOWN_SECONDS", "8"))
_last_request_at = defaultdict(float)  # user_id -> unix timestamp


# ---------------------------------------------------------------------------
# Per-user model preference (set via /model). "auto"/absent = PROVIDER_ORDER.
# ---------------------------------------------------------------------------
_user_provider = {}  # user_id -> provider name
# Tracks who opened each /model menu (message_id -> user_id). The keyboard sits on
# the bot's own reply message, so the owner can't be derived from the message.
_model_menu_owner = {}  # message_id -> user_id


def _prune_menu_owners() -> None:
    """Keeps the owner map from growing forever (dicts preserve insertion order)."""
    if len(_model_menu_owner) > 500:
        overflow = len(_model_menu_owner) - 500
        for key in list(_model_menu_owner)[:overflow]:
            _model_menu_owner.pop(key, None)


def _model_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Builds the model-picker inline keyboard. The user's current choice is
    marked with a checkmark; unconfigured providers are flagged with '(off)'."""
    current = _user_provider.get(user_id, "auto")
    rows = []
    for name in PROVIDER_ORDER:
        provider = PROVIDERS.get(name)
        if not provider:
            continue
        is_available, _, _ = provider
        label = f"{'✓ ' if name == current else ''}{name}"
        if not is_available():
            label += " (off)"
        rows.append([InlineKeyboardButton(label, callback_data=f"model:{name}")])
    rows.append([
        InlineKeyboardButton(
            "✓ auto" if current == "auto" else "auto",
            callback_data="model:auto",
        )
    ])
    return InlineKeyboardMarkup(rows)


def _on_cooldown(user_id: int) -> float:
    """Returns remaining cooldown seconds (0 if not on cooldown), and updates the timestamp if not."""
    now = time.time()
    elapsed = now - _last_request_at[user_id]
    if elapsed < COOLDOWN_SECONDS:
        return round(COOLDOWN_SECONDS - elapsed, 1)
    _last_request_at[user_id] = now
    return 0


# ---------------------------------------------------------------------------
# Short-term conversation memory (per chat) so the AI can keep up with a
# multi-user group conversation instead of answering each message in a vacuum.
# ---------------------------------------------------------------------------
MAX_HISTORY = 12                      # messages (user + bot) kept per chat
MAX_MEDIA_BYTES = 15 * 1024 * 1024    # cap for inline video/animation bytes

_histories = defaultdict(deque)       # chat_id -> deque[(display_name, text)]


def _record_history(chat_id: int, name: str, text: str) -> None:
    h = _histories[chat_id]
    h.append((name, text))
    while len(h) > MAX_HISTORY:
        h.popleft()


def _history_context(chat_id: int) -> str:
    h = _histories.get(chat_id)
    if not h:
        return ""
    return "\n".join(f"{name}: {text}" for name, text in h)


def _user_label(user) -> str:
    """Human-readable name + id so the AI can tell users apart. Prefers the
    user's exact @username when set (what people actually want to be called),
    falling back to display first/last name."""
    if not user:
        return "Unknown user"
    username = getattr(user, "username", None)
    if username:
        return f"@{username} (id {user.id})"
    name = (user.first_name or "").strip()
    if user.last_name:
        name = f"{name} {user.last_name}".strip()
    return f"{name or 'User'} (id {user.id})"


def _extract_media(bot: Bot, msg) -> list:
    """Downloads photo/video/animation from a message for multimodal Gemini."""
    parts = []
    if not msg:
        return parts
    try:
        if getattr(msg, "photo", None):
            file_obj = bot.get_file(msg.photo[-1].file_id)
            bio = io.BytesIO()
            file_obj.download(out=bio, timeout=60)
            data = bio.getvalue()
            if data:
                parts.append({"mime_type": "image/jpeg", "data": data})

        for attr in ("video", "animation"):
            obj = getattr(msg, attr, None)
            if obj and getattr(obj, "file_id", None):
                file_obj = bot.get_file(obj.file_id)
                bio = io.BytesIO()
                file_obj.download(out=bio, timeout=120)
                data = bio.getvalue()
                if data and len(data) <= MAX_MEDIA_BYTES:
                    parts.append({
                        "mime_type": getattr(obj, "mime_type", None) or "video/mp4",
                        "data": data,
                    })
    except Exception as e:
        LOGGER.warning(f"[ai] Failed to download media: {e}")
    return parts


# ---------------------------------------------------------------------------
# Core AI Functions
# ---------------------------------------------------------------------------

def _is_transient(err: Exception) -> bool:
    text = str(err).lower()
    return any(marker.lower() in text for marker in TRANSIENT_MARKERS)

def _model_not_found(err: Exception) -> bool:
    text = str(err).lower()
    return any(marker in text for marker in ("not found", "model not found", "404", "does not exist", "model_not_found", "permission denied"))

def _call_gemini(prompt: str, media=None) -> str:
    last_error = None
    models_to_try = [GEMINI_MODEL] + [
        m for m in GEMINI_FALLBACK_MODELS if m.lower() != GEMINI_MODEL.lower()
    ]
    for model in models_to_try:
        try:
            config = genai_types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)
            if media:
                contents = [
                    genai_types.Part.from_bytes(data=m["data"], mime_type=m["mime_type"])
                    for m in media
                ]
                contents.append(prompt)
            else:
                contents = prompt
            response = gemini_client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            return response.text.strip()
        except Exception as e:
            last_error = e
            if not _model_not_found(e):
                # Real failure (auth, quota, network) - do not mask it behind a
                # "model renamed" fallback; let the retry/failover layer handle it.
                raise
            LOGGER.warning(f"[ai] Gemini model '{model}' unavailable ({e}); trying fallback model.")
    if last_error is not None:
        raise last_error
    raise RuntimeError("Gemini call failed with no models available.")

def _call_mistral(prompt: str, media=None) -> str:
    if media:
        raise RuntimeError("Mistral provider does not support images/videos - use Gemini.")
    if not mistral_supports_chat_complete:
        raise RuntimeError(
            "mistralai SDK installed does not support chat.complete() - upgrade the package"
        )
    response = mistral_client.chat.complete(
        model=MISTRAL_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content.strip()

PROVIDERS = {
    "gemini": (lambda: gemini_client is not None, _call_gemini, True),
    "mistral": (lambda: mistral_client is not None and mistral_supports_chat_complete, _call_mistral, False),
}

def generate_ai_response(prompt: str, media=None, preferred=None) -> str:
    last_error = None
    tried_any = False

    # If the user picked a model via /model, try it first; if it gets exhausted
    # (rate limited / overloaded / down), we automatically pass to the next
    # provider in the order until one answers.
    order = []
    if preferred and preferred in PROVIDERS:
        order.append(preferred)
    for name in PROVIDER_ORDER:
        if name not in order:
            order.append(name)

    for provider_name in order:
        provider = PROVIDERS.get(provider_name)
        if not provider:
            LOGGER.warning(f"[ai] Unknown provider '{provider_name}' in AI_PROVIDER_ORDER, skipping.")
            continue

        is_available, call_fn, supports_media = provider
        if not is_available():
            continue

        if media and not supports_media:
            LOGGER.warning(f"[ai] {provider_name} does not support images/videos, skipping.")
            continue

        tried_any = True
        for attempt in range(1, MAX_RETRIES_PER_PROVIDER + 1):
            try:
                return call_fn(prompt, media)
            except Exception as e:
                last_error = e
                transient = _is_transient(e)
                LOGGER.warning(
                    f"[ai] {provider_name} attempt {attempt}/{MAX_RETRIES_PER_PROVIDER} failed "
                    f"({'transient, will retry' if transient else 'non-transient, giving up on this provider'}): {e}"
                )
                if not transient:
                    break
                if attempt < MAX_RETRIES_PER_PROVIDER:
                    time.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

        LOGGER.error(f"[ai] {provider_name} exhausted retries, moving to next provider if any.")

    if not tried_any:
        LOGGER.error("[ai] No AI providers are configured (missing API keys, or SDK incompatible).")
        return "I'm sorry, but my AI core is currently offline (no working providers configured)."

    LOGGER.error(f"[ai] All configured providers failed. Last error: {last_error}")
    return "Sorry, I had a brief neural misfire. Could you try asking that again?"

# ---------------------------------------------------------------------------
# YouTube Transcript Integration
# ---------------------------------------------------------------------------

def _join_transcript(data, max_chars: int = 15000) -> str:
    """Stitch transcript blocks into one string. Blocks may be objects with a
    .text attribute (older youtube-transcript-api) or dicts with 'text'."""
    parts = []
    for block in data:
        if isinstance(block, dict):
            text = block.get("text", "")
        else:
            text = getattr(block, "text", "")
        if text:
            parts.append(str(text))
    text = " ".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "... [Transcript truncated due to length]"
    return text


def _try_youtube_transcript_api(video_id: str) -> str:
    """Primary: the youtube-transcript-api library. Tolerates both the modern
    instance API and the older static API. Note: this is frequently IP-blocked
    on cloud hosts (Render, AWS, etc.), hence the fallbacks below."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        except AttributeError:
            transcript_list = YouTubeTranscriptApi().list(video_id)

        for transcript in transcript_list:
            data = transcript.fetch()
            if data:
                return _join_transcript(data)
    except ImportError:
        LOGGER.error("[ai] youtube-transcript-api is not installed!")
    except Exception as e:
        LOGGER.warning(f"[ai] youtube-transcript-api failed for {video_id}: {e}")
    return None


def _try_yt_dlp(video_id: str) -> str:
    """Fallback: yt-dlp with mobile player clients. The default 'web' client is
    what youtube-transcript-api uses and is frequently IP-blocked on cloud
    hosts; the android/ios clients are not (so far) and work from Render."""
    import tempfile
    import shutil
    import yt_dlp
    import json as _json

    tmpdir = tempfile.mkdtemp(prefix="yt_subs_")
    try:
        opts = {
            "skip_download": True,
            "writeautomaticsub": True,
            "writesubtitles": True,
            "subtitleslangs": ["en.*"],
            "subtitlesformat": "json3/vtt",
            "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)

        for path in sorted(glob.glob(os.path.join(tmpdir, "*.json3"))):
            with open(path, "r", encoding="utf-8") as fh:
                data = _json.load(fh)
            texts = []
            for ev in data.get("events", []):
                segs = ev.get("segs")
                if segs:
                    texts.append("".join(s.get("utf8", "") for s in segs))
            text = " ".join(texts).replace("\n", " ")
            if text.strip():
                return _join_transcript([{"text": text}])
        return None
    except ImportError:
        LOGGER.error("[ai] yt-dlp is not installed! (pip install yt-dlp) to enable this fallback.")
        return None
    except Exception as e:
        LOGGER.warning(f"[ai] yt-dlp transcript failed for {video_id}: {e}")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _try_youtubetotranscript(video_id: str) -> str:
    """Fallback: youtubetotranscript.com free endpoint. This is frequently behind
    a Cloudflare challenge from server IPs, so it's the last resort."""
    try:
        resp = requests.post(
            "https://youtubetotranscript.com/transcript",
            data={"video_id": video_id, "format": "true"},
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, list) and data:
            return _join_transcript(data)
    except Exception as e:
        LOGGER.warning(f"[ai] youtubetotranscript failed for {video_id}: {e}")
    return None


def _get_youtube_transcript(video_id: str) -> str:
    """Fetches a transcript using a fallback chain. On Render/cloud IPs the
    youtube-transcript-api requests are usually IP-blocked by YouTube, so we
    try yt-dlp (mobile clients) and public endpoints afterwards."""
    for fetcher in (_try_youtube_transcript_api, _try_yt_dlp, _try_youtubetotranscript):
        try:
            text = fetcher(video_id)
        except Exception as e:
            LOGGER.warning(f"[ai] {fetcher.__name__} crashed for {video_id}: {e}")
            continue
        if text:
            LOGGER.info(f"[ai] got transcript for {video_id} via {fetcher.__name__}")
            return text
    return None

def _get_youtube_context(video_id: str, url: str):
    """Fetch title/channel via oEmbed (no API key) plus the transcript."""
    title = None
    author = None
    try:
        resp = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            title = data.get("title")
            author = data.get("author_name")
    except Exception as e:
        LOGGER.debug(f"[ai] YouTube oEmbed lookup failed: {e}")

    transcript = _get_youtube_transcript(video_id)
    return title, author, transcript

def enhance_prompt_with_youtube(prompt: str) -> str:
    """Scans the prompt for a YouTube link, fetches title/channel + transcript,
    and silently injects them so the AI knows what the video is about."""
    yt_pattern = r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})'
    match = re.search(yt_pattern, prompt)

    if not match:
        return prompt

    video_id = match.group(1)
    video_url = match.group(0)
    title, author, transcript = _get_youtube_context(video_id, video_url)

    meta_lines = []
    if title:
        meta_lines.append(f"Title: {title}")
    if author:
        meta_lines.append(f"Channel: {author}")
    meta_str = "\n".join(meta_lines) or "Title: (unknown)"

    if transcript:
        note = (
            f"[System Note: A YouTube video was linked. Here is the hidden video context to analyze and answer the user's question:\n"
            f"{meta_str}\n\nTranscript:\n{transcript}]"
        )
    else:
        note = (
            f"[System Note: A YouTube video was linked, but its transcript is unavailable. Here is what is known about it:\n"
            f"{meta_str}\nIf the user asks about the video's content, explain you cannot watch it without a transcript.]"
        )

    return prompt + "\n\n" + note

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@run_async
def ask_ai(bot: Bot, update: Update, args):
    msg = update.effective_message
    user_id = msg.from_user.id
    query = " ".join(args)

    if not query:
        msg.reply_text("Please provide a question! Example: `/ask why is the sky blue?`", parse_mode=ParseMode.MARKDOWN)
        return

    remaining = _on_cooldown(user_id)
    if remaining:
        msg.reply_text(f"Slow down a bit! Try again in {remaining}s.")
        return

    media = _extract_media(bot, msg)
    if not media and msg.reply_to_message:
        media = _extract_media(bot, msg.reply_to_message)

    user_label = _user_label(msg.from_user)
    _record_history(msg.chat_id, user_label, query)

    context = _history_context(msg.chat_id)
    if context:
        prompt = f"Recent conversation in this chat:\n{context}\n\n{user_label} asked: {query}"
    elif msg.reply_to_message:
        context_text = msg.reply_to_message.caption or msg.reply_to_message.text
        if context_text:
            prompt = f"Previous message context:\n{context_text.strip()}\n\n{user_label} asked: {query}"
        else:
            prompt = f"{user_label} asked: {query}"
    else:
        prompt = f"{user_label} asked: {query}"

    prompt = enhance_prompt_with_youtube(prompt)

    bot.send_chat_action(chat_id=msg.chat_id, action="typing")
    response = generate_ai_response(prompt, media, preferred=_user_provider.get(user_id))
    _record_history(msg.chat_id, "Phoenix", response)
    msg.reply_text(response)

def _is_bot_mentioned(bot: Bot, msg) -> bool:
    """Checks real mention/text_mention entities rather than a raw substring search,
    which previously could false-positive on usernames that merely contain the
    bot's username as a substring. Works for captions on media messages too."""
    text = msg.text or msg.caption
    if not msg.entities or not text:
        return False
    for entity in msg.entities:
        if entity.type == "mention":
            mention_text = text[entity.offset: entity.offset + entity.length]
            if bot.username and mention_text.lower() == f"@{bot.username}".lower():
                return True
        elif entity.type == "text_mention" and entity.user and entity.user.id == bot.id:
            return True
    return False

@run_async
def mention_chatbot(bot: Bot, update: Update):
    msg = update.effective_message
    if not msg:
        return

    LOGGER.info("[ai] mention_chatbot handler fired: chat=%s user=%s text=%r",
                update.effective_chat.id,
                msg.from_user.id if msg.from_user else "?",
                (msg.text or msg.caption or "")[:80])

    is_pm = update.effective_chat.type == "private"

    is_reply_to_bot = bool(
        msg.reply_to_message
        and msg.reply_to_message.from_user
        and msg.reply_to_message.from_user.id == bot.id
    )

    is_mentioned = _is_bot_mentioned(bot, msg)
    media = _extract_media(bot, msg)

    # Bail out immediately if none of these apply - avoids spawning a thread
    # for every single message in a busy group (previous behavior).
    if not (is_pm or is_mentioned or is_reply_to_bot or media):
        return

    user_id = msg.from_user.id
    remaining = _on_cooldown(user_id)
    if remaining:
        # Stay quiet on cooldown for passive mentions/replies - only /ask gets
        # an explicit cooldown message, to avoid spamming a group chat.
        return

    LOGGER.info(
        f"[ai] mention_chatbot triggered by {user_id} in chat {msg.chat_id} "
        f"(PM: {is_pm}, Mention: {is_mentioned}, ReplyToBot: {is_reply_to_bot}, Media: {bool(media)})"
    )

    query = msg.text or msg.caption or ""
    if media and not query:
        query = "Please describe this image/video in detail."
    elif media and query:
        query = f"{query}\n\n(Also analyze the attached image/video.)"
    bot_username = f"@{bot.username}" if bot.username else ""
    if bot_username and bot_username.lower() in query.lower():
        query = re.sub(re.escape(bot_username), "", query, flags=re.IGNORECASE).strip()

    if not query:
        return

    user_label = _user_label(msg.from_user)
    _record_history(msg.chat_id, user_label, query)

    context = _history_context(msg.chat_id)
    if context:
        prompt = f"Recent conversation in this chat:\n{context}\n\n{user_label}: {query}"
    elif is_reply_to_bot and msg.reply_to_message:
        previous_text = msg.reply_to_message.caption or msg.reply_to_message.text
        if previous_text:
            prompt = f"Previous message context:\n{previous_text.strip()}\n\n{user_label}: {query}"
        else:
            prompt = f"{user_label}: {query}"
    else:
        prompt = f"{user_label}: {query}"

    prompt = enhance_prompt_with_youtube(prompt)

    bot.send_chat_action(chat_id=msg.chat_id, action="typing")
    response = generate_ai_response(prompt, media, preferred=_user_provider.get(user_id))
    _record_history(msg.chat_id, "Phoenix", response)
    msg.reply_text(response)

@run_async
def ai_status(bot: Bot, update: Update):
    lines = ["*AI provider status:*"]
    for name in PROVIDER_ORDER:
        provider = PROVIDERS.get(name)
        if not provider:
            lines.append(f"- `{name}`: unknown provider name")
            continue
        is_available, _, _ = provider
        status = "configured" if is_available() else "NOT configured (missing API key or incompatible SDK)"
        lines.append(f"- `{name}`: {status}")
    update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@run_async
def model_command(bot: Bot, update: Update, args):
    msg = update.effective_message
    user_id = msg.from_user.id
    arg = " ".join(args).strip().lower()

    if not arg:
        current = _user_provider.get(user_id, "auto")
        sent = msg.reply_text(
            f"Your current model: `{current}`\n\nTap a button to switch, or use `/model <name>` / `/model auto`.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_model_keyboard(user_id),
        )
        if sent:
            _model_menu_owner[sent.message_id] = user_id
            _prune_menu_owners()
        return

    if arg == "auto":
        _user_provider.pop(user_id, None)
        msg.reply_text("Model set to *auto* (automatic fallback order).", parse_mode=ParseMode.MARKDOWN)
        return

    provider = PROVIDERS.get(arg)
    if not provider:
        msg.reply_text(f"Unknown model `{arg}`. See `/model` for the list.", parse_mode=ParseMode.MARKDOWN)
        return
    is_available, _, _ = provider
    if not is_available():
        msg.reply_text(f"Model `{arg}` is not configured (missing API key or incompatible SDK).", parse_mode=ParseMode.MARKDOWN)
        return
    _user_provider[user_id] = arg
    msg.reply_text(
        f"Model set to `{arg}`. If it gets exhausted, the bot will automatically pass to the next provider.",
        parse_mode=ParseMode.MARKDOWN,
    )


@run_async
def model_callback(bot: Bot, update: Update):
    query = update.callback_query
    if not query or not query.data:
        return
    user_id = query.from_user.id
    # Only the user who opened the /model menu can change their own model. The
    # keyboard lives on the bot's reply message, so look up the owner by message_id.
    owner_id = _model_menu_owner.get(query.message.message_id, user_id) if query.message else user_id
    if query.from_user.id != owner_id:
        query.answer("This isn't your model menu!")
        return

    choice = query.data[len("model:"):].strip().lower()

    if choice == "auto":
        _user_provider.pop(user_id, None)
        label = "auto"
    elif choice in PROVIDERS:
        is_available, _, _ = PROVIDERS[choice]
        if not is_available():
            query.answer(f"{choice} is not configured")
            return
        _user_provider[user_id] = choice
        label = choice
    else:
        query.answer("Unknown model")
        return

    query.answer()
    query.edit_message_text(
        f"Model set to `{label}`. If it gets exhausted, the bot automatically passes to the next provider.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_model_keyboard(user_id),
    )


ASK_HANDLER = DisableAbleCommandHandler(["ask", "ai"], ask_ai, pass_args=True)
dispatcher.add_handler(ASK_HANDLER)

# NOTE: Filters.reply was intentionally removed from this filter - it matched
# ANY reply to ANY message, not specifically replies to the bot, causing this
# handler to fire (and spawn a thread) on every single reply in busy groups.
# The real reply-to-bot check happens inside mention_chatbot() via is_reply_to_bot.
MENTION_HANDLER = MessageHandler(
    Filters.text & ~Filters.command,
    mention_chatbot,
)
dispatcher.add_handler(MENTION_HANDLER, group=10)

# Multimodal: photos/videos/gifs sent to the bot (or replying to it) are also
# answered. The same is_pm/is_mentioned/is_reply_to_bot gate inside
# mention_chatbot() stops this from firing on random media in busy groups.
MEDIA_HANDLER = MessageHandler(
    Filters.photo | Filters.video | Filters.animation,
    mention_chatbot,
)
dispatcher.add_handler(MEDIA_HANDLER, group=13)

AI_STATUS_HANDLER = CommandHandler("aistatus", ai_status)
dispatcher.add_handler(AI_STATUS_HANDLER)

MODEL_HANDLER = DisableAbleCommandHandler("model", model_command, pass_args=True)
dispatcher.add_handler(MODEL_HANDLER)

MODEL_CALLBACK_HANDLER = CallbackQueryHandler(model_callback, pattern=r"^model:")
dispatcher.add_handler(MODEL_CALLBACK_HANDLER)

__help__ = """
Let's make the bot conversational! You can interact with the built-in AI model.

*Available commands:*
 - /ask <question>: Ask the AI any question directly.
 - /ai <question>: Same as /ask.
 - /model: Choose your preferred AI model (Gemini / Mistral / Auto) with an inline keyboard, or use `/model <name>`.
 - /aistatus: Shows which AI providers are configured and their fallback order.

*Alternative:*
- Simply tag the bot (`@bot_username`) in a group message, or message it in private, and it will automatically answer you using AI!

*Model selection:*
Run `/model` and tap a button to pick the provider used for your questions. Choose *Auto* (the default) to let the bot decide. If your chosen model gets exhausted (rate limited / overloaded), the bot automatically passes to the next available provider.

*Conversation memory:*
- The bot keeps track of the last several messages in a chat, so it can follow multi-turn conversations and tell users apart by name.

*Images & videos:*
- Send (or reply with) a photo, video, or GIF and the bot will describe and analyze it (Gemini only — Mistral has no vision).

*YouTube Support:*
If you send a YouTube link to the AI, it will fetch the video's title, channel, and closed captions and answer questions about the video!

*Reliability:*
This module automatically retries and fails over between providers (currently Gemini → Mistral, in that order) if one is temporarily overloaded. When images/videos are attached, only Gemini is used. Provider order is configurable via the `AI_PROVIDER_ORDER` environment variable.
"""

__mod_name__ = "AI Chatbot"
