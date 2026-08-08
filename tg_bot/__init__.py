import logging
import os
import re
import sys
import collections
import collections.abc

# Maintain legacy collection mappings for backward-compatible dependencies
collections.Mapping = collections.abc.Mapping
collections.MutableMapping = collections.abc.MutableMapping

import telegram.ext as tg
from loguru import logger


def _env_flag(name, default=False):
    """Parse an env boolean. '1/true/yes/on' (case-insensitive) are truthy;
    '0/false/no/off' and unset values are falsy (fixes bool('False') == True)."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int_set(name, default=""):
    """Parse an env int list that may use spaces or commas as separators."""
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    parts = [x for x in re.split(r"[,;\s]+", str(raw).strip()) if x]
    return {int(x) for x in parts}

class InterceptHandler(logging.Handler):
    LEVELS_MAP = {
        logging.CRITICAL: "CRITICAL",
        logging.ERROR: "ERROR",
        logging.WARNING: "WARNING",
        logging.INFO: "INFO",
        logging.DEBUG: "DEBUG"
    }

    def _get_level(self, record):
        return self.LEVELS_MAP.get(record.levelno, record.levelno)

    def emit(self, record):
        logger_opt = logger.opt(depth=6, exception=record.exc_info, ansi=True, lazy=True)
        logger_opt.log(self._get_level(record), record.getMessage())


logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO)

# enable logging
LOGGER = logging.getLogger(__name__)

# Verify Python runtime compatibility
if sys.version_info[0] < 3 or sys.version_info[1] < 7:
    LOGGER.error("You MUST have a python version of at least 3.7! Modern async features depend on this. Bot quitting.")
    quit(1)

ENV = _env_flag('ENV', False)

if ENV:
    TOKEN = os.environ.get('TOKEN', None)
    try:
        OWNER_ID = int(os.environ.get('OWNER_ID', None))
    except (ValueError, TypeError):
        raise Exception("Your OWNER_ID env variable is not a valid integer.")

    MESSAGE_DUMP = os.environ.get('MESSAGE_DUMP', None)
    OWNER_USERNAME = os.environ.get("OWNER_USERNAME", None)

    try:
        SUDO_USERS = _env_int_set("SUDO_USERS")
    except ValueError:
        raise Exception("Your sudo users list does not contain valid integers.")

    try:
        SUPPORT_USERS = _env_int_set("SUPPORT_USERS")
    except ValueError:
        raise Exception("Your support users list does not contain valid integers.")

    try:
        WHITELIST_USERS = _env_int_set("WHITELIST_USERS")
    except ValueError:
        raise Exception("Your whitelisted users list does not contain valid integers.")

    try:
        DEV_USERS = _env_int_set("DEV_USERS")
    except ValueError:
        raise Exception("Your developer users list does not contain valid integers.")

    WEBHOOK = _env_flag('WEBHOOK', False)
    URL = os.environ.get('URL', "")  # Does not contain token
    PORT = int(os.environ.get('PORT', 5000))
    CERT_PATH = os.environ.get("CERT_PATH")

    DB_URI = os.environ.get('DATABASE_URL')
    DONATION_LINK = os.environ.get('DONATION_LINK')
    LOAD = [x for x in os.environ.get("LOAD", "").split() if x]
    NO_LOAD = [x for x in os.environ.get("NO_LOAD", "translation").split() if x]
    DEL_CMDS = _env_flag('DEL_CMDS', False)
    STRICT_GBAN = _env_flag('STRICT_GBAN', False)
    WORKERS = int(os.environ.get('WORKERS', 8))
    BAN_STICKER = os.environ.get('BAN_STICKER', 'CAADAgADOwADPPEcAXkko5EB3YGYAg')
    ALLOW_EXCL = _env_flag('ALLOW_EXCL', False)
    LASTFM_API_KEY = os.environ.get('LASTFM_API_KEY', "")
    WALL_API = os.environ.get('WALL_API', "")
    MOE_API = os.environ.get('MOE_API', "")
    AI_API_KEY = os.environ.get('AI_API_KEY', "")
    MAL_CLIENT_ID = os.environ.get('MAL_CLIENT_ID', "")
    MAL_CLIENT_SECRET = os.environ.get('MAL_CLIENT_SECRET', "")
    MAL_ACCESS_TOKEN = os.environ.get('MAL_ACCESS_TOKEN', "")
    MAL_REFRESH_TOKEN = os.environ.get('MAL_REFRESH_TOKEN', "")
    MISTRAL_API_KEY = os.environ.get('MISTRAL_API_KEY', None)
    try:
        BL_CHATS = _env_int_set("BL_CHATS")
    except ValueError:
        raise Exception("Your blacklisted chats list does not contain valid integers.")

else:
    from tg_bot.config import Development as Config
    TOKEN = Config.API_KEY
    try:
        OWNER_ID = int(Config.OWNER_ID)
    except ValueError:
        raise Exception("Your OWNER_ID variable is not a valid integer.")

    MESSAGE_DUMP = Config.MESSAGE_DUMP
    OWNER_USERNAME = Config.OWNER_USERNAME

    try:
        SUDO_USERS = {int(x) for x in Config.SUDO_USERS or []}
    except ValueError:
        raise Exception("Your sudo users list does not contain valid integers.")

    try:
        SUPPORT_USERS = {int(x) for x in Config.SUPPORT_USERS or []}
    except ValueError:
        raise Exception("Your support users list does not contain valid integers.")

    try:
        WHITELIST_USERS = {int(x) for x in Config.WHITELIST_USERS or []}
    except ValueError:
        raise Exception("Your whitelisted users list does not contain valid integers.")

    try:
        DEV_USERS = {int(x) for x in Config.DEV_USERS or []}
    except ValueError:
        raise Exception("Your developer users list does not contain valid integers.")

    WEBHOOK = Config.WEBHOOK
    URL = Config.URL
    PORT = Config.PORT
    CERT_PATH = Config.CERT_PATH

    DB_URI = Config.SQLALCHEMY_DATABASE_URI
    DONATION_LINK = Config.DONATION_LINK
    LOAD = Config.LOAD
    NO_LOAD = Config.NO_LOAD
    DEL_CMDS = Config.DEL_CMDS
    STRICT_GBAN = Config.STRICT_GBAN
    WORKERS = Config.WORKERS
    BAN_STICKER = Config.BAN_STICKER
    ALLOW_EXCL = Config.ALLOW_EXCL
    LASTFM_API_KEY = Config.LASTFM_API_KEY
    WALL_API = Config.WALL_API
    MOE_API = Config.MOE_API
    AI_API_KEY = Config.AI_API_KEY
    MAL_CLIENT_ID = Config.MAL_CLIENT_ID
    MAL_ACCESS_TOKEN = Config.MAL_ACCESS_TOKEN
    MAL_REFRESH_TOKEN = Config.MAL_REFRESH_TOKEN
    MAL_CLIENT_SECRET = Config.MAL_CLIENT_SECRET
    MISTRAL_API_KEY = Config.MISTRAL_API_KEY
    try:
        BL_CHATS = {int(x) for x in Config.BL_CHATS or []}
    except ValueError:
        raise Exception("Your blacklisted chats list does not contain valid integers.")

# Establish administrative identities
if OWNER_ID:
    SUDO_USERS.add(OWNER_ID)
    DEV_USERS.add(OWNER_ID)

SUDO_USERS = list(SUDO_USERS)
WHITELIST_USERS = list(WHITELIST_USERS)
SUPPORT_USERS = list(SUPPORT_USERS)

# Legacy Application Initialization (PTB v11 compatible)
updater = tg.Updater(TOKEN, workers=WORKERS)
dispatcher = updater.dispatcher

# Load handlers at the end to ensure all core settings exist
from tg_bot.modules.helper_funcs.handlers import CustomCommandHandler, CustomRegexHandler, CustomMessageHandler

# Inject project-specific custom handler behavior overrides
tg.RegexHandler = CustomRegexHandler
tg.MessageHandler = CustomMessageHandler

# Always swap in the custom command handler so the !-prefix support and
# blacklist/flood protections apply. Gating it on ALLOW_EXCL meant that
# users who left ALLOW_EXCL unset silently lost all protections.
tg.CommandHandler = CustomCommandHandler
