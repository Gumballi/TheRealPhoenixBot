import threading

from sqlalchemy import Column, String, UnicodeText, func, distinct

from tg_bot.modules.sql import SESSION, BASE


class OwnerLock(BASE):
    __tablename__ = "owner_locked_users"
    chat_id = Column(String(14), primary_key=True)
    user_id = Column(String(14), primary_key=True)
    action = Column(UnicodeText, primary_key=True)

    def __init__(self, chat_id, user_id, action):
        self.chat_id = str(chat_id)
        self.user_id = str(user_id)
        self.action = action

    def __repr__(self):
        return "Owner-locked {} {} in {}".format(self.action, self.user_id, self.chat_id)


OwnerLock.__table__.create(checkfirst=True)
OWNER_LOCK_INSERTION_LOCK = threading.RLock()

OWNER_LOCKS = {}


def set_lock(chat_id, user_id, action):
    with OWNER_LOCK_INSERTION_LOCK:
        lock = SESSION.query(OwnerLock).get((str(chat_id), str(user_id), action))

        if not lock:
            OWNER_LOCKS.setdefault(str(chat_id), set()).add((str(user_id), action))

            lock = OwnerLock(str(chat_id), str(user_id), action)
            SESSION.add(lock)
            SESSION.commit()
            return True

        SESSION.close()
        return False


def remove_lock(chat_id, user_id, action):
    with OWNER_LOCK_INSERTION_LOCK:
        lock = SESSION.query(OwnerLock).get((str(chat_id), str(user_id), action))

        if lock:
            if (str(user_id), action) in OWNER_LOCKS.get(str(chat_id), set()):
                OWNER_LOCKS.setdefault(str(chat_id), set()).remove((str(user_id), action))

            SESSION.delete(lock)
            SESSION.commit()
            return True

        SESSION.close()
        return False


def is_locked(chat_id, user_id, action):
    return (str(user_id), action) in OWNER_LOCKS.get(str(chat_id), set())


def get_all_locked(chat_id):
    return OWNER_LOCKS.get(str(chat_id), set())


def num_chats():
    try:
        return SESSION.query(func.count(distinct(OwnerLock.chat_id))).scalar()
    finally:
        SESSION.close()


def num_locks():
    try:
        return SESSION.query(OwnerLock).count()
    finally:
        SESSION.close()


def migrate_chat(old_chat_id, new_chat_id):
    with OWNER_LOCK_INSERTION_LOCK:
        locks = SESSION.query(OwnerLock).filter(OwnerLock.chat_id == str(old_chat_id)).all()
        for lock in locks:
            lock.chat_id = str(new_chat_id)
            SESSION.add(lock)

        if str(old_chat_id) in OWNER_LOCKS:
            OWNER_LOCKS[str(new_chat_id)] = OWNER_LOCKS.get(str(old_chat_id), set())
            del OWNER_LOCKS[str(old_chat_id)]

        SESSION.commit()


def is_owner(update):
    from tg_bot import OWNER_ID
    user = update.effective_user
    return bool(user and user.id == OWNER_ID)


def can_act(bot, update, user_id, reverse_actions):
    """Guard for non-owner admins. If target user is owner-locked for any of
    the reverse actions, reply and return False (blocked). True = allowed."""
    from tg_bot import OWNER_ID
    user = update.effective_user
    if user and user.id == OWNER_ID:
        return True
    chat = update.effective_chat
    message = update.effective_message
    if user_id:
        for rev in reverse_actions:
            if is_locked(chat.id, user_id, rev):
                message.reply_text(
                    "This user was locked by the owner — only the owner can do that here.")
                return False
    return True


def owner_action(chat_id, user_id, action, reverse_action):
    """Called when the owner acts on a user. Clears the lock for the action they
    just took, and sets the lock for the reverse action."""
    remove_lock(chat_id, user_id, action)
    set_lock(chat_id, user_id, reverse_action)


def __load_owner_locks():
    global OWNER_LOCKS
    try:
        all_locks = SESSION.query(OwnerLock).all()
        for lock in all_locks:
            OWNER_LOCKS.setdefault(lock.chat_id, set()).add((lock.user_id, lock.action))
    finally:
        SESSION.close()


__load_owner_locks()
