import threading

from sqlalchemy import Column, String, UnicodeText, func, distinct

from tg_bot.modules.sql import SESSION, BASE


class Disable(BASE):
    __tablename__ = "disabled_commands"
    chat_id = Column(String(14), primary_key=True)
    command = Column(UnicodeText, primary_key=True)

    def __init__(self, chat_id, command):
        self.chat_id = chat_id
        self.command = command

    def __repr__(self):
        return "Disabled cmd {} in {}".format(self.command, self.chat_id)


class OwnerLock(BASE):
    __tablename__ = "owner_locked_commands"
    chat_id = Column(String(14), primary_key=True)
    command = Column(UnicodeText, primary_key=True)

    def __init__(self, chat_id, command):
        self.chat_id = chat_id
        self.command = command

    def __repr__(self):
        return "Owner-locked cmd {} in {}".format(self.command, self.chat_id)


Disable.__table__.create(checkfirst=True)
OwnerLock.__table__.create(checkfirst=True)
DISABLE_INSERTION_LOCK = threading.RLock()

DISABLED = {}
OWNER_LOCKED = {}


def disable_command(chat_id, disable):
    with DISABLE_INSERTION_LOCK:
        disabled = SESSION.query(Disable).get((str(chat_id), disable))

        if not disabled:
            DISABLED.setdefault(str(chat_id), set()).add(disable)

            disabled = Disable(str(chat_id), disable)
            SESSION.add(disabled)
            SESSION.commit()
            return True

        SESSION.close()
        return False


def enable_command(chat_id, enable):
    with DISABLE_INSERTION_LOCK:
        disabled = SESSION.query(Disable).get((str(chat_id), enable))

        if disabled:
            if enable in DISABLED.get(str(chat_id)):  # sanity check
                DISABLED.setdefault(str(chat_id), set()).remove(enable)

            SESSION.delete(disabled)
            SESSION.commit()
            return True

        SESSION.close()
        return False


def owner_lock_command(chat_id, command):
    with DISABLE_INSERTION_LOCK:
        locked = SESSION.query(OwnerLock).get((str(chat_id), command))

        if not locked:
            OWNER_LOCKED.setdefault(str(chat_id), set()).add(command)

            locked = OwnerLock(str(chat_id), command)
            SESSION.add(locked)
            SESSION.commit()
            return True

        SESSION.close()
        return False


def owner_unlock_command(chat_id, command):
    with DISABLE_INSERTION_LOCK:
        locked = SESSION.query(OwnerLock).get((str(chat_id), command))

        if locked:
            if command in OWNER_LOCKED.get(str(chat_id)):  # sanity check
                OWNER_LOCKED.setdefault(str(chat_id), set()).remove(command)

            SESSION.delete(locked)
            SESSION.commit()
            return True

        SESSION.close()
        return False


def is_owner_locked(chat_id, cmd):
    return str(cmd).lower() in OWNER_LOCKED.get(str(chat_id), set())


def get_all_owner_locked(chat_id):
    return OWNER_LOCKED.get(str(chat_id), set())


def is_command_disabled(chat_id, cmd):
    return str(cmd).lower() in DISABLED.get(str(chat_id), set())


def get_all_disabled(chat_id):
    return DISABLED.get(str(chat_id), set())


def num_chats():
    try:
        return SESSION.query(func.count(distinct(Disable.chat_id))).scalar()
    finally:
        SESSION.close()


def num_disabled():
    try:
        return SESSION.query(Disable).count()
    finally:
        SESSION.close()


def num_owner_locked():
    try:
        return SESSION.query(OwnerLock).count()
    finally:
        SESSION.close()


def migrate_chat(old_chat_id, new_chat_id):
    with DISABLE_INSERTION_LOCK:
        chats = SESSION.query(Disable).filter(Disable.chat_id == str(old_chat_id)).all()
        for chat in chats:
            chat.chat_id = str(new_chat_id)
            SESSION.add(chat)

        if str(old_chat_id) in DISABLED:
            DISABLED[str(new_chat_id)] = DISABLED.get(str(old_chat_id), set())
            del DISABLED[str(old_chat_id)]

        locks = SESSION.query(OwnerLock).filter(OwnerLock.chat_id == str(old_chat_id)).all()
        for lock in locks:
            lock.chat_id = str(new_chat_id)
            SESSION.add(lock)

        if str(old_chat_id) in OWNER_LOCKED:
            OWNER_LOCKED[str(new_chat_id)] = OWNER_LOCKED.get(str(old_chat_id), set())
            del OWNER_LOCKED[str(old_chat_id)]

        SESSION.commit()


def __load_disabled_commands():
    global DISABLED
    global OWNER_LOCKED
    try:
        all_chats = SESSION.query(Disable).all()
        for chat in all_chats:
            DISABLED.setdefault(chat.chat_id, set()).add(chat.command)

        all_locks = SESSION.query(OwnerLock).all()
        for lock in all_locks:
            OWNER_LOCKED.setdefault(lock.chat_id, set()).add(lock.command)

    finally:
        SESSION.close()


__load_disabled_commands()
