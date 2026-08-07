import time
import random
import logging
from telegram import Update
from telegram.ext import MessageHandler, Filters, run_async, DispatcherHandlerStop

from tg_bot import dispatcher

LOGGER = logging.getLogger(__name__)

@run_async
def command_chain_interceptor(bot, update: Update):
    msg = update.effective_message
    if not msg or not msg.text:
        return

    # Split the message into a list of commands using '&&' as the separator
    commands = [c.strip() for c in msg.text.split("&&") if c.strip()]

    # If there's no actual chaining happening, let normal processing continue
    if len(commands) < 2:
        return

    # Process each command separately
    for cmd in commands:
        # If the user forgot the slash on chained commands (e.g., "/warn @user && spank @user")
        if not cmd.startswith('/'):
            cmd = '/' + cmd

        try:
            # 1. Clone the entire update dictionary
            update_dict = update.to_dict()
            
            # 2. Overwrite the message text with our single stripped command
            update_dict['message']['text'] = cmd
            
            # 3. Randomize the update_id slightly so the dispatcher doesn't drop it as a duplicate
            update_dict['update_id'] = update.update_id + random.randint(1000, 9000)
            
            # 4. Convert the dictionary back into a real Telegram Update object
            new_update = Update.de_json(update_dict, bot)
            
            # 5. Feed the cloned, single-command update back into the top of the bot's dispatcher
            dispatcher.process_update(new_update)
            
            # Tiny anti-spam delay to prevent Telegram from throwing a 429 Flood Error
            time.sleep(1.0)
            
        except Exception as e:
            LOGGER.error(f"Error processing chained command '{cmd}': {e}")

    # Abort the original chained message so the bot doesn't try to process the giant combined string
    raise DispatcherHandlerStop()

# Register the handler in group -1 so it intercepts BEFORE normal commands (which run in group 0)
CHAIN_HANDLER = MessageHandler(Filters.regex(r'^/.*&&'), command_chain_interceptor)
dispatcher.add_handler(CHAIN_HANDLER, group=-1)

__help__ = """
 *Command Chaining:*
 You can run multiple commands at the exact same time using `&&`.
 
 *Example:* 
 `/warn @user && /spank @user && /react`
"""

__mod_name__ = "Chaining"

LOGGER.info("Command Chaining interceptor loaded successfully!")
