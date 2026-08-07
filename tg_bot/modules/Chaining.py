import time
import logging
import threading

from telegram import Update, MessageEntity
from telegram.ext import MessageHandler, Filters, DispatcherHandlerStop
from tg_bot import dispatcher

LOGGER = logging.getLogger(__name__)

MAX_CHAINED_COMMANDS = 5  # hard cap to prevent chain-length abuse / worker-thread starvation
CHAIN_DELAY_SECONDS = 1.0  # small delay between sub-command dispatches to avoid Telegram flood limits (429)

# Monotonically increasing counter used to build guaranteed-unique synthetic
# update_ids. Real Telegram update_ids are always positive, so keeping ours
# negative guarantees zero collision risk without relying on randomness.
_synthetic_id_lock = threading.Lock()
_synthetic_id_counter = 0


def _next_synthetic_update_id() -> int:
    global _synthetic_id_counter
    with _synthetic_id_lock:
        _synthetic_id_counter -= 1
        return _synthetic_id_counter


def _build_command_entity(cmd_text: str) -> dict:
    """Builds a fresh bot_command entity covering just the command token at
    offset 0. Required because CommandHandler.check_update() specifically
    requires entities[0].offset == 0 and type == 'bot_command' - reusing the
    original message's entities (which describe positions in the FULL
    &&-joined string) causes every command after the first to silently fail
    this check and never dispatch."""
    command_token = cmd_text.split()[0]
    return {
        "type": MessageEntity.BOT_COMMAND,
        "offset": 0,
        "length": len(command_token),
    }


def _run_chain(bot, original_update: Update, commands):
    """Runs in a dedicated background thread (NOT via @run_async on the
    handler itself) so the sleep-based flood delay never blocks the
    dispatcher's own update-processing loop, while still letting the calling
    handler raise DispatcherHandlerStop synchronously and effectively."""
    for cmd in commands:
        if not cmd.startswith('/'):
            cmd = '/' + cmd

        try:
            update_dict = original_update.to_dict()
            update_dict['message']['text'] = cmd
            update_dict['message']['entities'] = [_build_command_entity(cmd)]
            update_dict['update_id'] = _next_synthetic_update_id()

            new_update = Update.de_json(update_dict, bot)
            dispatcher.process_update(new_update)

            time.sleep(CHAIN_DELAY_SECONDS)

        except Exception as e:
            LOGGER.error(f"Error processing chained command '{cmd}': {e}")


def command_chain_interceptor(bot, update: Update):
    msg = update.effective_message
    if not msg or not msg.text:
        return

    commands = [c.strip() for c in msg.text.split("&&") if c.strip()]

    if len(commands) < 2:
        return

    if len(commands) > MAX_CHAINED_COMMANDS:
        msg.reply_text(f"Too many chained commands (max {MAX_CHAINED_COMMANDS}).")
        raise DispatcherHandlerStop()

    # Run the actual chain in a background thread so the per-command delay
    # doesn't block the dispatcher, but do NOT decorate this handler with
    # @run_async: DispatcherHandlerStop only works when raised synchronously,
    # on the same call stack the dispatcher used to reach this handler.
    threading.Thread(
        target=_run_chain,
        args=(bot, update, commands),
        daemon=True,
    ).start()

    # Abort further processing of the original, raw &&-joined message so it
    # doesn't ALSO get matched and executed (with mangled args) by a real
    # CommandHandler in group 0.
    raise DispatcherHandlerStop()


# Registered in group -1 so it intercepts BEFORE normal commands (group 0).
# Intentionally not wrapped in @run_async - see command_chain_interceptor docstring.
CHAIN_HANDLER = MessageHandler(Filters.regex(r'^/.*&&'), command_chain_interceptor)
dispatcher.add_handler(CHAIN_HANDLER, group=-1)

__help__ = """
 *Command Chaining:*
 You can run multiple commands at the exact same time using `&&`.

 *Example:*
 `/warn @user && /mute @user`

 Max {max_commands} chained commands per message.
""".format(max_commands=MAX_CHAINED_COMMANDS)

__mod_name__ = "Chaining"

LOGGER.info("Command Chaining interceptor loaded successfully!")
